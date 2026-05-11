# Host heartbeat watchdog
#
# Sends a periodic `host_heartbeat` command to every MCU. If the host
# process dies, hangs, or the USB link goes silent, each MCU will trip
# its own shutdown after `timeout` seconds (see src/host_watchdog.c).
#
# Optional: if [host_watchdog] is absent from the config, behavior is
# unchanged.
#
# Copyright (C) 2026  Kalico contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging


# Default timeout: 500ms. Rationale (see DESIGN.md):
#  - Typical reactor loop iteration: <1ms
#  - CPython GC pause (gen-2) on a Pi 4: occasionally 50-150ms
#  - USB scheduling latency on a hot host: tens of ms
#  - We send heartbeats every timeout/4 (i.e. 125ms), giving four
#    chances to land before the MCU trips. That tolerates one missed
#    heartbeat plus jitter without nuisance shutdowns.
DEFAULT_TIMEOUT = 0.500

# Heartbeat divider: send heartbeats this many times per timeout
# window. 4 means "if any 4 consecutive heartbeats are missed, trip".
HEARTBEAT_DIVIDER = 4

# Minimum interval between heartbeats. We never send faster than this,
# even if someone configures an absurdly low timeout, because the cost
# is bus bandwidth on every MCU.
MIN_HEARTBEAT_INTERVAL = 0.010


class _McuWatchdog:
    """Per-MCU state. One of these is created for each MCU."""

    def __init__(self, host_wd, mcu):
        self._host_wd = host_wd
        self._mcu = mcu
        self._oid = mcu.create_oid()
        self._cmd_queue = mcu.alloc_command_queue()
        self._heartbeat_cmd = None
        self._start_cmd = None
        self._stop_cmd = None
        self._armed = False
        mcu.register_config_callback(self._build_config)

    def _build_config(self):
        mcu = self._mcu
        timeout_ticks = mcu.seconds_to_clock(self._host_wd.timeout)
        # Initial config sent on every connect / firmware_restart.
        mcu.add_config_cmd(
            "config_host_watchdog oid=%d timeout_ticks=%d"
            % (self._oid, timeout_ticks)
        )
        # Make sure a stale watchdog from a previous run is stopped on
        # firmware restart. start happens later from `_ready`, after
        # the reactor heartbeat loop is registered.
        mcu.add_config_cmd(
            "host_watchdog_stop oid=%d" % (self._oid,),
            on_restart=True,
        )
        self._heartbeat_cmd = mcu.lookup_command(
            "host_heartbeat oid=%c", cq=self._cmd_queue
        )
        self._start_cmd = mcu.lookup_command(
            "host_watchdog_start oid=%c", cq=self._cmd_queue
        )
        self._stop_cmd = mcu.lookup_command(
            "host_watchdog_stop oid=%c", cq=self._cmd_queue
        )

    def start(self):
        # Send a heartbeat *first* so last_heartbeat is fresh, then
        # arm. The MCU start command sets last_heartbeat = now itself,
        # but doing this here too gives a deterministic ordering even
        # if the queue is reordered.
        self._heartbeat_cmd.send([self._oid])
        self._start_cmd.send([self._oid])
        self._armed = True

    def heartbeat(self):
        if not self._armed:
            return
        # Low-latency send: no reqclock, no minclock, no retry quirks
        # -- this is the fast path. reqclock=0 makes the serialqueue
        # ship it as soon as bus bandwidth allows.
        try:
            self._heartbeat_cmd.send([self._oid])
        except Exception:
            # Don't let a serial blip kill the host process. If the
            # connection is truly dead, the MCU will trip its own
            # watchdog and we'll see the shutdown via the normal path.
            logging.exception(
                "host_watchdog: heartbeat send failed for mcu %s",
                self._mcu.get_name(),
            )

    def stop(self):
        if not self._armed:
            return
        self._armed = False
        try:
            self._stop_cmd.send([self._oid])
        except Exception:
            # Best-effort. If the MCU is gone we don't care.
            logging.debug(
                "host_watchdog: stop send failed for mcu %s",
                self._mcu.get_name(),
                exc_info=True,
            )


class HostWatchdog:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.timeout = config.getfloat("timeout", DEFAULT_TIMEOUT, above=0.0)
        interval = self.timeout / HEARTBEAT_DIVIDER
        self.heartbeat_interval = max(MIN_HEARTBEAT_INTERVAL, interval)
        self._mcu_wds = []
        self._heartbeat_timer = None
        self._running = False
        # Register early -- klippy:mcu_identify fires before
        # klippy:connect and is when MCUs exist in the printer object
        # table but before config callbacks run.
        self.printer.register_event_handler(
            "klippy:mcu_identify", self._handle_mcu_identify
        )
        self.printer.register_event_handler(
            "klippy:ready", self._handle_ready
        )
        self.printer.register_event_handler(
            "klippy:shutdown", self._handle_shutdown
        )
        self.printer.register_event_handler(
            "klippy:disconnect", self._handle_disconnect
        )

    def _handle_mcu_identify(self):
        # Enumerate every MCU (primary + secondaries) and attach a
        # watchdog to each. Skip MCUs flagged non-critical -- those
        # are allowed to drop out without taking the printer with
        # them, so a host watchdog there would be counter-productive.
        for name, mcu in self.printer.lookup_objects(module="mcu"):
            if getattr(mcu, "is_non_critical", False):
                logging.info(
                    "host_watchdog: skipping non-critical MCU '%s'",
                    mcu.get_name(),
                )
                continue
            self._mcu_wds.append(_McuWatchdog(self, mcu))
        logging.info(
            "host_watchdog: armed for %d MCU(s), timeout=%.3fs,"
            " heartbeat_interval=%.3fs",
            len(self._mcu_wds),
            self.timeout,
            self.heartbeat_interval,
        )

    def _handle_ready(self):
        for wd in self._mcu_wds:
            wd.start()
        self._running = True
        # register_timer fires immediately the first time so we get an
        # early heartbeat in addition to the one sent from start().
        self._heartbeat_timer = self.reactor.register_timer(
            self._heartbeat_event, self.reactor.NOW
        )

    def _heartbeat_event(self, eventtime):
        if not self._running:
            return self.reactor.NEVER
        for wd in self._mcu_wds:
            wd.heartbeat()
        # Re-arm. Using eventtime + interval (not monotonic() +
        # interval) lets the reactor catch up after jitter without
        # drifting -- if we were late, the next call comes sooner.
        return eventtime + self.heartbeat_interval

    def _stop(self):
        if not self._running:
            return
        self._running = False
        if self._heartbeat_timer is not None:
            self.reactor.unregister_timer(self._heartbeat_timer)
            self._heartbeat_timer = None
        for wd in self._mcu_wds:
            wd.stop()

    def _handle_shutdown(self):
        # Klippy is going down (either via M112 from the user, an MCU
        # trip we didn't cause, or anything else). Disarm cleanly so
        # we don't trip a *second* shutdown on the MCUs that aren't
        # the one that caused this one.
        self._stop()

    def _handle_disconnect(self):
        # Final teardown. Best-effort -- if the MCU watchdog already
        # tripped, the connection is already torn down and the stop
        # sends will silently fail.
        self._stop()

    def get_status(self, eventtime):
        return {
            "timeout": self.timeout,
            "heartbeat_interval": self.heartbeat_interval,
            "running": self._running,
            "mcus": [wd._mcu.get_name() for wd in self._mcu_wds],
        }


def load_config(config):
    return HostWatchdog(config)
