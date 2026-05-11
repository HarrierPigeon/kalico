# Hardware emergency-stop input.
#
# Configures a GPIO pin on an MCU as a dedicated, autonomous emergency-stop
# input. The MCU firmware polls the pin and calls shutdown() the moment a
# trigger is observed - the host is not involved in the trip path. This
# module's only job is to configure the pin at connect time and to surface
# its state via klippy's status object.
#
# See the [hardware_estop] section description in DESIGN.md for wiring
# expectations (NC vs NO, pull-ups, redundant contacts).
#
# Copyright (C) 2026  Kalico contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


QUERY_INTERVAL = 1.0  # seconds between background state queries


class HardwareEstop:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        # Parse pin. Use can_invert/can_pullup so the user can also write
        # `pin: ^!estop` style shortcuts if they prefer.
        ppins = self.printer.lookup_object("pins")
        pin_params = ppins.lookup_pin(
            config.get("pin"), can_invert=True, can_pullup=True
        )
        self.mcu = pin_params["chip"]
        self.mcu_name = pin_params["chip_name"]
        self.pin = pin_params["pin"]
        # Resolve pull-up. The pin-prefix `^` / `~` (parsed as pin_params
        # "pullup" of 1 / -1) takes precedence if the user supplied one;
        # otherwise honor the explicit `pullup:` boolean (default True for
        # safety - a floating input would falsely read as triggered).
        if pin_params["pullup"]:
            self.pullup = pin_params["pullup"]
        else:
            self.pullup = 1 if config.getboolean("pullup", True) else 0
        # invert=True means "closed contact == safe, open contact == trip".
        # This is the recommended setting for industrial NC switches.
        # The `!` pin prefix is XOR'd in for consistency with the rest of
        # the codebase.
        self.invert = config.getboolean("invert", False)
        if pin_params["invert"]:
            self.invert = not self.invert
        # trigger_value is the *logical* level on the MCU pin that means
        # "tripped". With pull-up + NC-to-ground wiring and invert=True,
        # the pin is held low (val=0) when safe and floats high (val=1)
        # when tripped -> trigger_value=1.
        self.trigger_value = 1 if self.invert else 0

        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self._build_config)

        # Per-MCU shutdown notifications - we use these to mark our status
        # as "triggered" when our shutdown reason is the cause.
        self.mcu.register_response(
            self._handle_estop_state, "estop_input_state", self.oid
        )

        # Local mirror of MCU state, updated by background queries.
        self._last_pin_value = None
        self._last_triggered = False
        self._query_cmd = None

        self.printer.register_event_handler(
            "klippy:ready", self._handle_ready
        )
        self.printer.register_event_handler(
            "klippy:disconnect", self._handle_disconnect
        )

    def _build_config(self):
        self.mcu.add_config_cmd(
            "config_estop_input oid=%d pin=%s pull_up=%d trigger_value=%d"
            % (self.oid, self.pin, self.pullup, self.trigger_value)
        )
        cmd_queue = self.mcu.alloc_command_queue()
        self._query_cmd = self.mcu.lookup_command(
            "query_estop_input oid=%c", cq=cmd_queue
        )

    def _handle_ready(self):
        # Kick off a periodic state query so klippy's status object reflects
        # the live pin level. The MCU side does NOT depend on these queries
        # for safety - they exist solely for observability.
        self.reactor.register_timer(
            self._do_query, self.reactor.monotonic() + QUERY_INTERVAL
        )

    def _handle_disconnect(self):
        # Nothing to undo on the MCU side - the firmware will keep watching
        # the pin until reset, which is the desired behavior.
        self._query_cmd = None

    def _do_query(self, eventtime):
        if self._query_cmd is None:
            return self.reactor.NEVER
        try:
            self._query_cmd.send([self.oid])
        except Exception:
            logging.exception("hardware_estop: query send failed")
        return eventtime + QUERY_INTERVAL

    def _handle_estop_state(self, params):
        self._last_pin_value = params.get("pin_value", 0)
        self._last_triggered = bool(params.get("triggered", 0))

    def get_status(self, eventtime=None):
        # `armed` means the MCU is actively watching the pin (i.e. we got at
        # least one state report back). `triggered` means the MCU's firmware
        # has called shutdown() on our behalf. `pin_value` is the raw level.
        return {
            "armed": self._query_cmd is not None,
            "triggered": self._last_triggered,
            "pin_value": self._last_pin_value,
            "pin": self.pin,
            "invert": self.invert,
            "pullup": self.pullup,
        }


def load_config(config):
    return HardwareEstop(config)
