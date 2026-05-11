# Host Heartbeat Watchdog -- Design

Status: prototype. Optional, off by default. One of several parallel
estop approaches under evaluation; the others (hardware estop input on
the MCU, M112 path improvements, lookahead bound enforcement) address
different threat models and are complementary, not competing.

## Problem

Today, if the Klippy host process dies (Python crash, OOM kill, USB
unplug, OS hang, deadlock in a klippy module), the MCU has no way to
know. The motion timers it already scheduled keep firing, the heater
PWM keeps running at its last commanded duty, and any buffered moves
play out to completion. On a typical desktop printer this might be 1-2
seconds of motion; on a large/industrial machine with deep lookahead
buffers and a slow toolhead it can be much longer. For an industrial
machine that is unacceptable -- a host crash must hard-stop the
machine within a tight, predictable bound.

## What this adds

A simple, strict, host-driven heartbeat:

1. Host sends `host_heartbeat oid=<n>` to every MCU every
   `timeout / 4` seconds.
2. Each MCU tracks `last_heartbeat = timer_read_time()` in its
   `host_watchdog` task.
3. If `(now - last_heartbeat) > timeout_ticks` on any armed watchdog,
   the MCU calls `shutdown("Host heartbeat lost")` -- the same code
   path used by `M112` / `emergency_stop`. All `DECL_SHUTDOWN`
   handlers run (steppers stop, heaters off, fans to safe state, etc.)
   via the longjmp through `sched_shutdown`.

The host module is loaded only when `[host_watchdog]` appears in
printer.cfg. Absent that, behavior is unchanged.

## Why this complements (rather than replaces) other estop paths

| Failure                                       | Hardware estop button | M112 (gcode) | Host watchdog |
|-----------------------------------------------|:---------------------:|:------------:|:-------------:|
| Operator hits the red button                  |          yes          |       -      |       -       |
| Operator types `M112`                         |           -           |      yes     |       -       |
| Host process killed by OOM                    |           -           |       -      |      yes      |
| Host Python crash / unhandled exception       |           -           |       -      |      yes      |
| Host OS hang (kernel deadlock, freeze)        |           -           |       -      |      yes      |
| USB cable unplugged                           |           -           |       -      |      yes      |
| Reactor stuck in an infinite loop in a module |           -           |       -      |      yes      |
| Toolhead crashes into bed at correct G-code   |          yes          |     (slow)   |       -       |
| Heater runaway with host alive                |           -           |       -      |       -       |

The host watchdog covers exactly the gap between "host is alive and
sending sensible commands" and "MCU is alive and trusted to keep
running". It does not detect host bugs that happen *while sending
heartbeats* -- those are the domain of heater runaway protection,
limit switches, hardware estop, etc.

## Failure modes covered

- **Python interpreter crash** (segfault, SIGABRT from C ext): reactor
  loop stops; no more heartbeats; MCU trips in <= `timeout`.
- **OOM kill**: kernel sends SIGKILL; same as above.
- **USB unplug / link death**: heartbeats can't reach the MCU; MCU
  trips on its own clock. (This is the path-redundant case: the
  serialhdl timeout would also notice, but the MCU is doing the
  detecting itself, which is the safety property we want.)
- **OS hang / kernel freeze**: the host's userspace doesn't tick;
  heartbeats stop; MCU trips.
- **Infinite loop in a klippy module on the reactor thread**: the
  reactor's timer wheel doesn't advance, the heartbeat timer doesn't
  fire, MCU trips.
- **Klippy in shutdown state without notifying the MCU**: we send a
  best-effort `host_watchdog_stop` from `klippy:shutdown` /
  `klippy:disconnect`. If those handlers ran, the watchdog is disarmed
  cleanly. If they didn't (e.g. process killed), the MCU trips.

## Failure modes NOT covered (intentionally)

- Host is alive and sending heartbeats but the printer is misbehaving
  (wrong G-code, bad config, bad thermal model, crashed toolhead with
  no probe). That's what M112, hardware estop, max_temp / min_temp,
  and probe verification are for.
- An MCU that is too busy in IRQ context to ever run the watchdog
  task. The task runs in the main scheduling loop, woken by a
  periodic timer; an MCU that fails to schedule the timer at all is
  already in a `Timer too close` shutdown state. The sentinel timer
  in `sched.c` guarantees this.
- An MCU whose USB stack accepts the heartbeat into its serial buffer
  but never schedules the command parser. This would be caught by the
  watchdog only if the parser is genuinely starved; in practice a
  starved parser also means motion commands aren't being processed
  and the machine isn't moving.

## Choosing `timeout`

Default: **500ms** with heartbeat every **125ms** (4x headroom).

What sets the floor:

- **CPython GC pauses**: on a Pi 4 / Pi 5 running klippy, generation-2
  collections can pause the reactor for 50-150ms under typical load.
  The reactor explicitly runs gen-0 and gen-1 only when idle, but gen-2
  is implicit. A 200ms timeout is realistic only if you disable gen-2
  GC (`gc.disable()` + manual collection); we don't.
- **USB scheduling**: on a busy host with other USB devices, the
  bulk-out endpoint can sit in the queue for 10-30ms before the kernel
  hands it to the device.
- **Reactor jitter**: a busy reactor with many timers and a slow MCU
  serial sync can drift each iteration by 1-5ms.
- **CAN bus contention**: for CAN-bridged MCUs, arbitration delays can
  add another few ms.

A 500ms timeout with 125ms heartbeats means three consecutive
heartbeats must be missed to trip. That's nearly always GC + USB
queueing under genuinely abnormal conditions, not steady state.

What sets the ceiling:

- The whole point is to bound the post-crash motion window. 500ms at
  100mm/s toolhead is 50mm of travel, which is acceptable for most
  machines. For an industrial machine where 50mm is *not* acceptable,
  tighten to 200ms (heartbeat at 50ms) and accept the risk of nuisance
  trips during GC. Below 100ms total, you will see false positives on
  a stock Pi.

This is configurable per installation:

```
[host_watchdog]
timeout: 0.250
```

## Reset / clear semantics

After a `Host heartbeat lost` shutdown, recovery is the same as any
other MCU shutdown:

1. Klippy logs the shutdown reason and goes to shutdown state.
2. User runs `FIRMWARE_RESTART` (or restarts klippy with
   `RESTART`, depending on whether the host process itself is in a
   sane state).
3. On restart, the host module re-issues `config_host_watchdog`,
   `host_watchdog_stop` (on_restart), and `host_watchdog_start` once
   the reactor is ready.

The MCU side also runs `host_watchdog_shutdown` (a `DECL_SHUTDOWN`
handler) which disarms all watchdog instances. Combined with
`sched_timer_reset()`'s wipe of the timer list, this means a stale
poll timer cannot fire post-shutdown.

## Reactor-side safety

The heartbeat callback is registered with `reactor.register_timer`,
which returns a `waketime` for the next call. We return
`eventtime + heartbeat_interval` (not `monotonic() + interval`) so
that:

- If the reactor was late dispatching us, the next call comes
  proportionally sooner -- we don't drift forward.
- Each call decides its own next wakeup, so a stuck callback can't
  pile up duplicate timers.

The heartbeat send path catches exceptions and logs them rather than
re-raising. If the serial link is truly dead the MCU will trip on its
own clock; we don't want a transient `SerialError` in the host module
to take down klippy in a way that *also* prevents the cleanup.

## Multi-MCU

Every MCU registered in the printer object table gets its own
watchdog OID. Non-critical MCUs (`is_non_critical: true`) are
skipped, because the whole point of marking an MCU non-critical is
that the host is *allowed* to lose contact with it without halting
the printer.

Each MCU's heartbeat goes out in the same `_heartbeat_event` reactor
tick, so a single reactor stall produces a single round of MCU trips
rather than a cascading one. Each MCU's watchdog tripping
independently is the desired property: a CAN-bridged toolhead MCU
that loses contact with the host should shut down even if the main
MCU somehow doesn't.

## Known limitations

- `FIRMWARE_RESTART` is fine -- the `on_restart` config command
  issues `host_watchdog_stop` before `_send_config` re-arms anything.
- If the user issues `RESET` (the low-level MCU reset) without going
  through klippy, the MCU comes up unconfigured and the watchdog is
  not running. The host will re-config on reconnect; there is a brief
  window between MCU boot and host reconnect during which no watchdog
  is active. That window already exists for every other safety
  feature and is not unique to this design.
- The 10ms internal poll interval on the MCU adds at most ~10ms of
  detection latency on top of the configured `timeout`. This is
  already accounted for in the suggested defaults.

## Files

- `src/host_watchdog.c` -- MCU module
- `src/Makefile` -- build wiring (`src-y`, always built)
- `klippy/extras/host_watchdog.py` -- host module
- `config/example-extras.cfg` -- sample config snippet
- `docs/Config_Reference.md` -- user-facing reference
