# Hardware GPIO Emergency-Stop Input (Design A)

## Problem

Kalico's existing `M112` path queues an emergency-stop request like any other
gcode command: it goes through the host's event loop, gets serialized, sent
to the MCU, parsed, and only then does the MCU call `shutdown()`. For
industrial machinery (CNCs, large-format printers, anything that can hurt
someone) this is unsafe: a hung host process or a saturated serial link
silently disables the stop button.

## Approach

A physical emergency-stop button is wired directly to a GPIO pin on the MCU.
The MCU firmware polls that pin every 1 ms in its own scheduler tick. The
moment a trip is observed (and confirmed by ~3 ms of debouncing), the
firmware calls `shutdown("Emergency stop button pressed")` *from inside the
MCU*. The host is informed via the standard shutdown message, but the host
is **not** on the critical path: even if Klippy is dead, crashed, or
disconnected, the button still stops motion.

```
Button ──[GPIO pin]──> MCU firmware (estop_input.c)
                            │
                            ├─ polls pin every 1 ms
                            ├─ debounces (3 consecutive matching samples)
                            └─ on trip: shutdown("Emergency stop button pressed")
                                   │
                                   ├─ stepper_shutdown() clears move queue,
                                   │   stops timers, drops DIR pins
                                   ├─ pwm/gpio shutdown handlers fire
                                   └─ MCU sends `shutdown` message to host
```

The host-side `klippy/extras/hardware_estop.py` only does configuration: at
connect time it issues `config_estop_input` so the MCU knows which pin to
watch and which level means "tripped". It also periodically queries pin
state for `printer.hardware_estop` status reporting. None of this is
required for the stop to work — it's all observability.

## Wiring expectations

The recommended wiring is an **industrial normally-closed (NC) contact** to
ground, with the MCU's internal pull-up enabled:

```
    MCU 3.3V
       │
      [internal pull-up]
       │
       ├──> MCU GPIO pin
       │
    [NC button]   <-- pressed = opens circuit
       │
      GND
```

With this wiring:

- Button **not pressed** (safe): switch is closed → pin is pulled LOW.
- Button **pressed** (trip): switch opens → pull-up pulls pin HIGH.
- **Wire cut, connector unplugged, button damaged**: pin floats HIGH → trip.
- **Power lost to button circuit**: same as above → trip.

This is the classic "fail-safe" pattern: any failure of the safety circuit
itself is indistinguishable from the button being pressed, and trips the
machine.

The Klipper config for this wiring looks like:

```ini
[hardware_estop]
pin: PA10
pullup: True
invert: True
```

`invert: True` is what tells the host that "pin HIGH means tripped". The
host computes `trigger_value = 1` and passes it to the MCU.

If for some reason you must use a normally-open (NO) button — strongly
discouraged for safety, but supported for completeness — set `invert: False`
(the default). Pressing the button then connects the pin to ground and the
MCU trips on LOW.

### Redundant contacts

A single switch is one bounce, one corroded contact, or one stuck welded
relay away from failing in the "doesn't trip" direction. Real industrial
machines wire **two redundant NC contacts** in series on a single safety
circuit (ISO 13849-1 / EN 60204-1 Cat 2+). With both contacts in series, a
single contact failure still allows the other to break the circuit and trip
the stop.

This design supports redundant contacts by wiring them in series on a
single pin. To monitor each contact independently (for diagnostics — e.g.
detecting one welded contact before the next press), declare multiple
`[hardware_estop NAME]` sections (one per contact); each one is a separate
MCU oid and runs its own poll timer. **The current implementation does not
do force-guided contactor monitoring**; see Limitations below.

## What the host sees

When the button is pressed, the sequence is:

1. MCU timer fires `estop_event()`, reads pin, finds trigger value for 3
   consecutive samples.
2. MCU calls `shutdown("Emergency stop button pressed")` which is a longjmp
   to `run_shutdown()` (`src/sched.c:296`).
3. `run_shutdown()` walks the `DECL_SHUTDOWN` chain — `stepper_shutdown()`
   (`src/stepper.c:384`) clears move queues, kills the stepper timer, sets
   all DIR pins low. PWM and GPIO output shutdown handlers also fire.
4. MCU transmits a `shutdown` message to the host with the static string id
   for "Emergency stop button pressed". Klippy logs this and propagates it
   through `_handle_shutdown` in `klippy/mcu.py:1255`.
5. The host's `printer.hardware_estop` status (`get_status()`) reports
   `triggered: True` after the next periodic query lands (~1 s).
6. The user (or Mainsail/Fluidd) sees the MCU in shutdown state with the
   reason "Emergency stop button pressed", makes that exact string easy to
   match for any operator UI that wants to display "ESTOP PRESSED" instead
   of a generic shutdown message.

Recovery is the same as any other shutdown: release the button, then
`FIRMWARE_RESTART`. The MCU clears its state and re-arms automatically (the
host re-issues `config_estop_input` during `_build_config`).

## Files

- `src/estop_input.c` — MCU-side polling, debouncing, and `shutdown()` call.
- `src/Makefile` — adds `estop_input.c` under `CONFIG_WANT_ESTOP_INPUT`.
- `src/Kconfig` — defines `WANT_ESTOP_INPUT` (default y when GPIO is
  available; user-toggleable in `make menuconfig`).
- `klippy/extras/hardware_estop.py` — parses the `[hardware_estop]` section,
  issues `config_estop_input` at connect time, exposes status.
- `config/sample-hardware-estop.cfg` — example config snippet.

## Limitations & known issues

1. **No force-guided contactor (FGC) monitoring.** A real safety-rated stop
   circuit includes an auxiliary contact on the master contactor that is
   force-mechanically linked to the main contacts; the controller monitors
   this to detect a welded contact. We do not do this. If you need an
   IEC 60947-5-1 compliant stop, run a real safety relay (e.g.
   Pilz PNOZ s3) and use Kalico's button as a *secondary* signaling input
   only.

2. **Single MCU only per `[hardware_estop]` section.** Each section
   configures one pin on one MCU. If you have multiple MCUs (CAN-bus
   toolheads, etc.) you need a separate `[hardware_estop NAME]` for each.
   The host-side coordination is just "if any MCU shuts down, all stop" via
   Klippy's normal shutdown propagation.

3. **MCU-only scope.** This trips the *MCU*. A separate hardware contactor
   on the printer's main power input is what physically removes power from
   the steppers/heaters. The MCU-side shutdown stops motion commands, but
   only a real contactor (driven by the same safety circuit) guarantees
   power is gone. Recommended wiring: the same NC button breaks both the
   contactor coil circuit and the MCU GPIO, in series.

4. **Debounce time is hard-coded** at ~3 ms (3 samples × 1 ms poll). This
   is appropriate for industrial mushroom-head buttons (which bounce for
   ≤1 ms) but could be parameterized in a follow-up if a user has a noisy
   switch.

5. **No persistent latching.** Once the button is released and
   `FIRMWARE_RESTART` is issued, the system clears. There is no
   "key-to-reset" behavior — that would need an additional reset input.
   For safety standards that require positive operator action to clear
   (i.e. the operator must affirmatively reset, not just release), wrap
   this in a key switch in the same series circuit.

6. **The MCU's emergency_stop command path is unchanged.** Existing
   `M112`/host-initiated stops still work exactly as before; this is a
   parallel, additional trigger that runs entirely on the MCU.

## Why not interrupts?

Most Kalico-supported MCUs *do* support external interrupts on GPIO pins,
and an interrupt-driven trigger would have ~microsecond latency vs the ~3 ms
of this polled design. However:

- The polling-and-debouncing approach uses zero board-specific code; it
  works the same on AVR, STM32, RP2040, LPC, SAMD, HC32, and the host
  simulator with no per-arch porting.
- 3 ms is well below the response time of a stepper move (which can take
  tens to hundreds of ms to overshoot when current is cut anyway, depending
  on inertia), so the extra latency is not safety-relevant in practice.
- Polling is much easier to reason about for code review: there's no
  shared state between an ISR and the main loop, no need to think about
  reentrancy of `shutdown()` from an interrupt context.

If a future user needs sub-millisecond latency, an interrupt-based
back-end could be added per-arch as an optimization without changing the
host-side interface.
