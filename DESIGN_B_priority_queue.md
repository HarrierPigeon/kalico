# Design: Serialqueue Priority Lane (estop bypass)

Status: working prototype on branch `tests/estop-interrupt`. One of several
parallel approaches under evaluation; this one targets the serialqueue
batching layer in `klippy/chelper/serialqueue.c`.

## Problem

Kalico's host-side MCU command pipeline funnels every outbound message
through a single C-level queue:

```
caller (Python) ──▶ serialqueue_send() ──▶ cq->upcoming_queue
                                                │
                                  check_send_command() (clock gated)
                                                │
                                          cq->ready_queue
                                                │
                                  command_event() batches up to
                                  MAX_PENDING_BLOCKS × MESSAGE_MAX bytes
                                                │
                                          do_write()  ──▶  USB / CAN
```

`emergency_stop` is sent as a normal command with `min_clock=0, reqclock=0`.
Those values give it a high logical priority inside `build_and_send_command()`
(lowest `req_clock` wins), so it becomes the first message inside the next
batch — but it still has to wait for:

1. The bg thread to wake (next pollreactor tick or kick).
2. The current `command_event()` invocation to finish any batch it is
   already building.
3. The kernel's USB scheduler to pick up the resulting block.

When the toolhead is mid-print, the queue is dense with `queue_step` and
trapq-driven moves. The emergency_stop byte ends up at the front of a
~720-byte (12 × 64) framed payload that has to fully serialize over USB
before the MCU sees it. At typical USB poll intervals (~1 ms) plus
batching jitter, this can add several ms of latency to an action that
should be effectively instantaneous.

We want a true bypass — an "urgent" lane that skips both the per-queue
clock scheduler **and** the batching pass.

## Design

Add a global priority list directly on `struct serialqueue` (not on any
`command_queue`, because this is meant to override normal queueing
completely):

```c
struct serialqueue {
    ...
    struct list_head priority_queue;   // global priority bypass lane
    int priority_bytes;
    ...
};
```

New public API:

```c
void serialqueue_send_priority(struct serialqueue *sq, uint8_t *msg, int len);
```

Behavior:

1. Allocate a `queue_message`, copy `msg` into it.
2. Under `sq->lock`: append it to `priority_queue`, set
   `need_kick_clock = 0`, and `pollreactor_update_timer(SQPT_COMMAND, PR_NOW)`.
3. Release lock and `kick_bg_thread()` to wake the reactor if it is
   sleeping in `poll()`.

The transmission side adds one helper, `drain_priority_queue()`, called
from the very top of `command_event()` **before** the existing
`check_send_command()` loop. For each priority message it:

1. Builds a single-message framed block in a 64-byte stack buffer:
   `MESSAGE_HEADER_SIZE | payload | MESSAGE_TRAILER_SIZE` with the
   normal sequence byte, CRC, and SYNC trailer.
2. Calls `do_write(sq, buf, len)` **immediately** — no batching with
   any other message.
3. Pushes a copy of the framed block onto `sent_queue` with the
   appropriate `send_seq`, increments `send_seq` / `need_ack_bytes`,
   and arms the retransmit timer if `sent_queue` was empty.

After the priority drain returns, the normal `check_send_command()` /
`build_and_send_command()` loop runs untouched. Normal-path semantics
(clock-gated upcoming → ready transitions, batching up to
`MAX_PENDING_BLOCKS`, ack accounting against `MAX_PENDING_BLOCKS` and
`receive_window`) are preserved.

### Measured / estimated latency improvement

The bypass eliminates two pieces of latency:

| Source                                    | Typical magnitude  | Bypassed? |
|-------------------------------------------|--------------------|-----------|
| Python `raw_send` → C `serialqueue_send`  | <100 µs            | no        |
| FFI thread wakeup (`kick_bg_thread`)      | ~10–100 µs         | no        |
| Wait for already-building batch to finish | up to ~1 ms        | **yes**   |
| Slot behind same-batch messages in USB    | up to ~0.7 ms      | **yes**   |
| Kernel USB poll interval                  | ~1 ms              | no        |

So under load the win is roughly **1–2 ms of host-side latency
eliminated**, dropping the dominant remaining contributor to the USB
poll interval itself (which only a USB transfer-priority change could
move further). On idle/low-traffic links the bypass is a tiny pessimism
(we drop the ~1ms batching opportunity), but the trade is intentional —
emergency_stop is a one-shot, the next message-event will rebatch.

### Retransmit / ack semantics — choice (b)

The constraint document offered two options:

- **(a)** Fire-and-forget: send, don't add to `sent_queue`. Lower code
  cost, but the MCU's reliable-transport state machine is left
  inconsistent — the next normal message would advance `send_seq` past
  the priority byte, the MCU would NAK based on its own sequence
  expectation, and we'd never retransmit the estop.
- **(b)** Properly track in `sent_queue`. Slightly more code; estop
  participates in normal retransmit.

**This prototype implements (b).** Specifically `drain_priority_queue()`:

- Increments `sq->send_seq` and `sq->need_ack_bytes`.
- Allocates a tracking `queue_message`, copies the framed bytes,
  records `sent_time = eventtime`, and appends to `sent_queue`.
- Arms `SQPT_RETRANSMIT` via `pollreactor_update_timer` if `sent_queue`
  was previously empty.
- Sets `rtt_sample_seq` if it was clear so the RTT estimator stays
  populated.

Justification for the extra code: emergency_stop is precisely the
command for which we **cannot** afford "fire and forget" semantics. A
dropped emergency_stop byte that never retransmits means the printer
keeps running through a fault. We pay the ~20 lines of bookkeeping.

The notify-id path is also handled: if a priority message ever carries
`notify_id != 0` (not used by the estop wiring, but the API permits it),
it is parked on `notify_queue` with `req_clock = send_seq - 1`, the same
convention as `build_and_send_command()`.

### Threading & locking

All `priority_queue` / `priority_bytes` accesses are under `sq->lock`:

- `serialqueue_send_priority` — locks before list append and before
  poking `need_kick_clock` / pollreactor timer.
- `drain_priority_queue` — called only from `command_event` which
  already holds `sq->lock`.
- `serialqueue_free` — locks before `message_queue_free(&sq->priority_queue)`
  alongside the other queues.

`kick_bg_thread()` writes a byte to the self-pipe and is intentionally
called **after** dropping the lock, mirroring `serialqueue_send_batch()`.
That avoids holding the lock across a syscall and is safe because the
pollreactor will simply observe the new state on the next pass once
woken.

`do_write()` is invoked **while** holding `sq->lock`. That is identical
to the existing `command_event()` write — the lock is held across the
whole batching/write pass — so this introduces no new lock-vs-syscall
ordering surprises. A pathological USB stall would block all serial
traffic regardless of which lane the message took.

### Sequence number ordering

Each priority block is written before any later normal block is built,
so its `send_seq` is strictly less than any subsequent normal block in
the same `command_event` tick. If `command_event()` is mid-batch when
`drain_priority_queue` is reached, the call site (the top of
`command_event`) ensures we have not yet built any normal block, so the
sequence ordering on the wire matches `sent_queue` ordering, which is
what `update_receive_seq()` expects.

## Python plumbing

`klippy/chelper/__init__.py` cdef block gets one extra line declaring
`serialqueue_send_priority`.

`klippy/serialhdl.py` adds:

```python
def raw_send_priority(self, cmd):
    self._check_noncritical_disconnected()
    if self.serialqueue is None:
        return
    self.ffi_lib.serialqueue_send_priority(
        self.serialqueue, cmd, len(cmd)
    )
```

No `command_queue`, no `minclock`, no `reqclock` — it really is "send
this now."

## MCU wiring

`MCU._shutdown` (klippy/mcu.py ~line 1391) now encodes the
`emergency_stop` command and hands the bytes to `raw_send_priority`:

```python
def _shutdown(self, force=False):
    if self._emergency_stop_cmd is None or (
        self._is_shutdown and not force
    ):
        return
    cmd_bytes = self._emergency_stop_cmd._cmd.encode(())
    raw_send_priority = getattr(self._serial, "raw_send_priority", None)
    if raw_send_priority is not None:
        raw_send_priority(cmd_bytes)
    else:
        self._emergency_stop_cmd.send()
```

The `getattr` fallback exists for safety during partial rebuilds (e.g.
a stale `c_helper.so` from before the symbol was added). In a fully
deployed build the priority path is always taken.

## End-to-end flow (M112)

```
G-code processor reads "M112"
  │
  ▼
gcode.py:451   GCodeDispatch.cmd_M112(...)
  │
  ▼
printer.py:471 Printer.invoke_shutdown("Shutdown due to M112 command")
  │
  ▼
for cb in event_handlers["klippy:shutdown"]: cb()
  │ ... (other shutdown handlers run synchronously here) ...
  ▼
mcu.py:_shutdown()
  │
  ▼
emergency_stop bytes = cmd._cmd.encode(())
  │
  ▼
serialhdl.SerialReader.raw_send_priority(cmd_bytes)
  │
  ▼ (FFI)
serialqueue_send_priority(sq, msg, len)
  │
  ▼ (under sq->lock)
list_add_tail(priority_queue) + kick reactor
  │
  ▼ (bg thread, next command_event tick)
drain_priority_queue() → do_write(USB) directly + push to sent_queue
```

### Residual issue (out of scope for this design)

`invoke_shutdown` walks `event_handlers["klippy:shutdown"]` **in
registration order, synchronously**. Any handler registered before
`MCU._shutdown` runs to completion before our priority byte even
enters the C queue. That latency is *not* what this design targets —
this design optimizes only the serialqueue-batching segment of the
critical path. Eliminating the synchronous-handler delay is the job
of a separate approach (design F in the parallel-evaluation set):
either reordering callbacks so MCU shutdown fires first, or splitting
"send the bytes now" out from "tear down state" so the wire transmit
starts before the rest of the shutdown sequence proceeds.

Documenting this here so reviewers know the priority-lane work and the
handler-ordering work are complementary, not competing.

## Configuration

None. The priority lane is plumbed in unconditionally:

- The C-level `priority_queue` always exists; it is empty on a healthy
  printer.
- Only `MCU._shutdown()` currently submits to it, so the only command
  type that takes the lane is `emergency_stop`. Adding more callers in
  the future is intentionally a code change, not a config change —
  flooding the priority lane would defeat its purpose.

## Files changed

- `klippy/chelper/serialqueue.c` — `priority_queue` field, init/free,
  `drain_priority_queue()`, `serialqueue_send_priority()`,
  `command_event()` drains priority first.
- `klippy/chelper/serialqueue.h` — prototype for
  `serialqueue_send_priority`.
- `klippy/chelper/__init__.py` — cdef entry for the new function.
- `klippy/serialhdl.py` — `SerialReader.raw_send_priority(cmd)`.
- `klippy/mcu.py` — `MCU._shutdown` routes through the priority lane,
  with `getattr` fallback.

## Build verification

- `gcc -Wall -g -O2 -fPIC -c serialqueue.c` — clean.
- Full c_helper.so link (all 19 source files with `-flto -march=native`)
  — clean.
- `nm -D` confirms `serialqueue_send_priority` is exported as a
  global text symbol.
- cffi cdef block from `klippy/chelper/__init__.py` parses cleanly
  and `dlopen()` resolves the new symbol.
