// Host heartbeat watchdog
//
// The host sends a `host_heartbeat` command periodically. If the gap
// between heartbeats exceeds the configured timeout (in clock ticks),
// the MCU calls shutdown("Host heartbeat lost") -- the same code path
// used by emergency_stop. This protects against host-side failures
// (Python crash, OOM, USB unplug, OS hang, etc.) that would otherwise
// leave the MCU running buffered moves with no supervision.
//
// This is NOT a substitute for M112 / hardware estop -- it only
// detects the host going silent. See DESIGN.md for the full threat
// model.
//
// Copyright (C) 2026  Kalico contributors
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc, oid_lookup, foreach_oid
#include "board/irq.h" // irq_disable / irq_enable
#include "board/misc.h" // timer_read_time, timer_is_before, timer_from_us
#include "command.h" // DECL_COMMAND, shutdown
#include "sched.h" // DECL_TASK, struct timer, sched_wake_task

// Per-watchdog state. One instance per host_watchdog OID. In the
// common single-MCU case there is exactly one; we still support
// multiple OIDs (e.g. per-host or per-mcu-bridge instances) without
// special-casing.
struct host_watchdog {
    struct timer poll_timer;     // periodic wake for the task
    uint32_t timeout_ticks;      // configured deadline (clock ticks)
    uint32_t last_heartbeat;     // timer_read_time() at last heartbeat
    uint8_t flags;
};

enum { HWF_RUNNING = 1 << 0 };

// How often the polling timer wakes the task. The actual deadline
// check is done against `timeout_ticks`, so this only bounds the
// detection latency -- 10ms gives us at least 10ms resolution on a
// 50ms heartbeat / 500ms timeout pair, which is far below the
// host-side jitter we expect.
#define WATCHDOG_POLL_US 10000

static struct task_wake host_watchdog_wake;

// Periodic timer that wakes the watchdog task. Re-armed each poll.
static uint_fast8_t
host_watchdog_poll_event(struct timer *t)
{
    sched_wake_task(&host_watchdog_wake);
    t->waketime += timer_from_us(WATCHDOG_POLL_US);
    return SF_RESCHEDULE;
}

void
command_config_host_watchdog(uint32_t *args)
{
    struct host_watchdog *hw = oid_alloc(
        args[0], command_config_host_watchdog, sizeof(*hw));
    hw->timeout_ticks = args[1];
    hw->poll_timer.func = host_watchdog_poll_event;
    // Not armed yet -- host_watchdog_start arms it.
}
DECL_COMMAND(command_config_host_watchdog,
             "config_host_watchdog oid=%c timeout_ticks=%u");

// Update the heartbeat timestamp. This is the hot-path command --
// keep it cheap. We deliberately do NOT mark it HF_IN_SHUTDOWN; once
// shutdown has run, late heartbeats should be silently dropped (the
// MCU is no longer trusted to be doing anything else), and the host
// should be observing the shutdown state on its own.
void
command_host_heartbeat(uint32_t *args)
{
    struct host_watchdog *hw = oid_lookup(
        args[0], command_config_host_watchdog);
    irq_disable();
    if (hw->flags & HWF_RUNNING)
        hw->last_heartbeat = timer_read_time();
    irq_enable();
}
DECL_COMMAND(command_host_heartbeat, "host_heartbeat oid=%c");

void
command_host_watchdog_start(uint32_t *args)
{
    struct host_watchdog *hw = oid_lookup(
        args[0], command_config_host_watchdog);
    if (!hw->timeout_ticks)
        shutdown("host_watchdog timeout not configured");
    irq_disable();
    // If already running, stop and re-arm cleanly so the new start
    // gets a fresh deadline without two poll timers stacking up.
    if (hw->flags & HWF_RUNNING)
        sched_del_timer(&hw->poll_timer);
    hw->last_heartbeat = timer_read_time();
    hw->flags |= HWF_RUNNING;
    hw->poll_timer.waketime =
        hw->last_heartbeat + timer_from_us(WATCHDOG_POLL_US);
    sched_add_timer(&hw->poll_timer);
    irq_enable();
}
DECL_COMMAND(command_host_watchdog_start, "host_watchdog_start oid=%c");

void
command_host_watchdog_stop(uint32_t *args)
{
    struct host_watchdog *hw = oid_lookup(
        args[0], command_config_host_watchdog);
    irq_disable();
    if (hw->flags & HWF_RUNNING) {
        hw->flags &= ~HWF_RUNNING;
        sched_del_timer(&hw->poll_timer);
    }
    irq_enable();
}
// Allow stop to run even after shutdown so a clean disconnect from a
// host that already saw a shutdown does not itself error.
DECL_COMMAND_FLAGS(command_host_watchdog_stop, HF_IN_SHUTDOWN,
                   "host_watchdog_stop oid=%c");

// The task scans every armed watchdog OID and trips shutdown if any
// of them has gone past its deadline. Running in task context (not
// timer/IRQ) means shutdown() does its normal longjmp through
// run_shutdown -- same code path as command_emergency_stop.
void
host_watchdog_task(void)
{
    if (!sched_check_wake(&host_watchdog_wake))
        return;
    uint8_t oid;
    struct host_watchdog *hw;
    foreach_oid(oid, hw, command_config_host_watchdog) {
        irq_disable();
        uint8_t flags = hw->flags;
        uint32_t last = hw->last_heartbeat;
        uint32_t timeout = hw->timeout_ticks;
        irq_enable();
        if (!(flags & HWF_RUNNING))
            continue;
        uint32_t now = timer_read_time();
        // (now - last) > timeout, expressed safely against wrap. The
        // deadline is `last + timeout`; if `now` is at-or-after the
        // deadline, trip. timer_is_before(now, deadline) is true
        // while we are still within the window.
        uint32_t deadline = last + timeout;
        if (!timer_is_before(now, deadline))
            shutdown("Host heartbeat lost");
    }
}
DECL_TASK(host_watchdog_task);

// On any shutdown (including ours), disarm all watchdogs so a stale
// poll timer can't fire during shutdown processing or after a
// clear_shutdown / config_reset.
void
host_watchdog_shutdown(void)
{
    uint8_t oid;
    struct host_watchdog *hw;
    foreach_oid(oid, hw, command_config_host_watchdog) {
        hw->flags &= ~HWF_RUNNING;
        // sched_timer_reset() already cleared the timer list in
        // run_shutdown(); don't double-delete.
    }
}
DECL_SHUTDOWN(host_watchdog_shutdown);
