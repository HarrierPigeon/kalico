# Design F — Host-side fast path for emergency stop

Status: prototype, this branch.

This is one of several parallel designs being evaluated to reduce the
latency between an M112 (or other emergency-stop trigger) and the MCU
actually halting motion. Design F is intentionally the smallest of the
five: surgical, low-risk, host-side-only changes that pull tens of
milliseconds of Python work out of the critical path.

## Problem recap

When `M112` arrives at the Klippy host today:

1. `GCodeIO._process_data()` (`klippy/gcode.py`) reads a chunk from the
   gcode FD and splits it into lines.
2. A regex sniff for `M112` is run **only** when
   `1 < len(pending_commands) < 20`. If the buffer holds exactly one
   pending command (the M112 itself, in the common case) or more than
   twenty, the sniff is skipped and the command is processed in order —
   i.e. after every previously queued g-code line.
3. Eventually `cmd_M112` fires and calls `Printer.invoke_shutdown()`
   (`klippy/printer.py:471`).
4. `invoke_shutdown` walks `self.event_handlers["klippy:shutdown"]`
   **synchronously, in registration order**. There are many handlers
   (heaters, thermistors, TMC drivers, BME280, ADC sensors, fans,
   neopixel, etc.). `MCU._shutdown` — the handler that actually sends
   `emergency_stop` bytes down serialqueue to the firmware — is just one
   of them, and depending on module load order it may run after dozens
   of others.

Each handler is short on its own, but a typical config has dozens. In
aggregate they add tens of milliseconds of Python work before the byte
that stops the printer hits the wire.

## What this design changes

Three surgical edits plus a belt-and-suspenders extra:

### 1. Widen the M112 sniff (`klippy/gcode.py`)

The M112 regex is now run on **every** chunk of freshly-read lines,
unconditionally, before any other processing.

- Previously gated by `1 < len(pending_commands) < 20`.
- The old gate skipped the sniff for the most common case (a single
  M112 sitting alone in the buffer) — that lone command was processed
  in-order, which is fine when the buffer ahead of it is empty but
  pays nothing in the case where it isn't.
- The old gate also skipped the sniff when the buffer was *large*,
  which is exactly when out-of-order processing matters most (a long
  preloaded gcode file or a frontend that flushes a big batch).
- M112 is idempotent — `Printer.invoke_shutdown` checks
  `self.in_shutdown_state` on entry and bails on repeats — so
  double-firing (sniff + in-order processing) is safe.

### 2. Priority-registered MCU shutdown handler (`klippy/printer.py`, `klippy/mcu.py`)

Added `Printer.register_priority_event_handler(event, callback)` which
inserts the callback at the **front** of the handler list for `event`.

`MCU.__init__` now registers its `_shutdown` via this priority API for
`klippy:shutdown` (only). Every other event registration (`connect`,
`disconnect`, `ready`, …) uses the normal API and retains the original
ordering semantics.

Net effect: when `invoke_shutdown` dispatches the `klippy:shutdown`
event, every MCU's `_shutdown` runs first, which sends `emergency_stop`
on each MCU's command queue *before* any heater/sensor/TMC handler gets
a Python opcode of work.

### 3. Belt-and-suspenders: direct MCU shutdown in `invoke_shutdown`

Just before `invoke_shutdown` iterates the handler list, it now also
walks `self.lookup_objects(module="mcu")` and calls `_shutdown()`
directly on each MCU. This is redundant with #2 in normal operation —
`MCU._shutdown` is idempotent (checks `self._is_shutdown`) so the
handler-loop call is a no-op — but it guarantees the bytes hit
serialqueue even if priority registration is bypassed by a future
refactor or a third-party module that doesn't know about the priority
API.

Cost: one extra Python loop over a list of length ~1-3 and one
idempotent method call per MCU. Negligible.

## What this design does NOT change

### Async shutdown path

`Printer.invoke_async_shutdown` (`klippy/printer.py:486`) defers the
real `invoke_shutdown` via `reactor.register_async_callback`. This path
is taken when shutdown is triggered from a non-reactor thread, mostly
from MCU response callbacks reporting firmware errors. The reactor
scheduling delay can be 1-10ms.

**This design leaves the async path untouched.** Changing it would
require either signal-safe shortcuts or reactor reordering, both of
which risk reentrancy bugs in the rest of the system. For software-
triggered emergencies (an exception in a Python module), the existing
async path is acceptable. The synchronous M112 path is where users feel
the latency.

### Toolhead / move queue draining

The MCU side will execute every move byte already sitting in the USB
FIFO and the firmware step compress queue before honoring
`emergency_stop`. Typical drain can be 100-300ms of motion. **Design F
does not address this** — that's Design B's territory (e.g. firmware
preempt, USB FIFO flush, hardware E-stop line).

### "Host died entirely" failure mode

If the host crashes, freezes, or is OOM-killed, none of this code runs.
The firmware watchdog will eventually time out (~100ms) and halt the
MCU, but that's the existing fallback. Design D adds hardware/firmware
heartbeating to close that hole.

## Estimated latency savings

Order-of-magnitude estimate, based on typical handler counts and Python
function-call overhead:

- **M112 sniff widening:** saves the average in-order queue depth times
  per-line processing cost. For a frontend that flushes batches, this
  can be tens of milliseconds; for an idle prompt it's near zero.
- **MCU._shutdown prioritization:** ~10-40ms on a typical config with
  many heaters/sensors/TMCs. Each non-MCU `klippy:shutdown` handler is
  a few hundred microseconds to a couple of milliseconds (most do
  `set_temp(0)` or similar, plus logging). With 20-40 handlers ahead
  of MCU._shutdown today, moving MCU._shutdown to the front saves the
  full cumulative cost.
- **Belt-and-suspenders direct MCU shutdown:** redundant in the happy
  path, free in the common case.

Combined, **tens of milliseconds** of host-side latency reduction is a
fair claim. Not a headline number — but meaningful, and effectively
free.

## Recommendation: defense in depth

Design F is the cheapest and safest of the five candidates. It should
ship even if a more aggressive design is chosen as the primary, because
its risk surface is tiny and the savings stack with everything else:

- **Pair with Design A** (e.g. dedicated reactor priority for E-stop):
  Design F removes Python work from the critical path; A makes sure
  what's left runs immediately.
- **Pair with Design B** (USB FIFO / firmware preempt): F handles the
  host side; B handles the queued-move side. They address orthogonal
  delays.
- **Pair with Design D** (hardware/firmware heartbeat): F is useless if
  the host has died; D backstops that case.

Together: F + B + D covers the three independent latency sources
(host-side Python, queued moves, host failure) without any single
design having to do everything.

## Edge cases considered

- **Idempotent M112:** verified — `invoke_shutdown` short-circuits on
  `in_shutdown_state`, so the sniff firing AND the in-order processor
  later firing the same command is harmless.
- **`lookup_objects("mcu")` before MCUs exist:** returns `[]`, the loop
  is a no-op. `invoke_shutdown` is only ever called once the printer
  has at least started reading config, by which point MCUs are added
  via `mcu.add_printer_objects`.
- **Multiple MCUs:** `lookup_objects(module="mcu")` returns the
  primary `mcu` plus every `mcu <name>`; all are shutdown in one pass.
- **Priority handler vs. normal handler ordering between two priority
  registrations:** the LAST priority-registered callback ends up at
  index 0 (LIFO among priority handlers). For our use this only
  matters if multiple modules ever priority-register `klippy:shutdown`;
  currently only MCU does. Fine for the prototype.
- **Third-party modules that subclass MCU or register their own
  shutdown:** unaffected — they continue to use
  `register_event_handler`, just no longer run *before* MCU._shutdown.
  This is the intended behavior change.
- **Tests:** the existing test suite doesn't exercise the M112 sniff
  branch with timing assertions, and `event_handlers` ordering is not
  asserted anywhere. No test changes required for prototype.
- **Idempotency of MCU._shutdown:** verified at `klippy/mcu.py:1391` —
  bails if `_emergency_stop_cmd is None` or `_is_shutdown` is set.

## Files changed

- `klippy/gcode.py` — unconditional M112 sniff in `_process_data`.
- `klippy/printer.py` — `register_priority_event_handler` method;
  belt-and-suspenders direct MCU shutdown call in `invoke_shutdown`.
- `klippy/mcu.py` — use priority registration for MCU `klippy:shutdown`.
- `DESIGN.md` — this file.
