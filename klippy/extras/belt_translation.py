# Belt printer coordinate transform
#
# Linear (affine) rotation around the X axis, mapping slicer-Cartesian
# coordinates (XY = ground plane, Z = up) to belt-printer machine
# coordinates (XY = gantry plane, Z = belt direction).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math


class BeltTranslation:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.belt_angle = config.getfloat(
            "belt_angle", above=5.0, below=175.0
        )
        self.belt_direction = config.getchoice(
            "belt_direction",
            {"+1": 1, "-1": -1, "1": 1},
            "+1",
        )
        self._update_trig()
        self.next_transform = None
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "SET_BELT_ANGLE",
            self.cmd_SET_BELT_ANGLE,
            desc=self.cmd_SET_BELT_ANGLE_help,
        )
        gcode.register_command(
            "GET_BELT_ANGLE",
            self.cmd_GET_BELT_ANGLE,
            desc=self.cmd_GET_BELT_ANGLE_help,
        )

    def _handle_connect(self):
        gcode_move = self.printer.lookup_object("gcode_move")
        self.next_transform = gcode_move.set_move_transform(self, force=True)

    def _update_trig(self):
        rad = math.radians(self.belt_angle)
        self.sin_a = math.sin(rad)
        self.cos_a = math.cos(rad)
        self.cot_a = self.cos_a / self.sin_a

    def move(self, newpos, speed):
        x_s, y_s, z_s, e = newpos
        y_m = z_s / self.sin_a
        z_m = y_s - self.belt_direction * z_s * self.cot_a
        self.next_transform.move([x_s, y_m, z_m, e], speed)

    def get_position(self):
        x_m, y_m, z_m, e = self.next_transform.get_position()
        y_s = z_m + self.belt_direction * y_m * self.cos_a
        z_s = y_m * self.sin_a
        return [x_m, y_s, z_s, e]

    cmd_SET_BELT_ANGLE_help = (
        "Set belt printer gantry angle (degrees from horizontal)"
    )

    def cmd_SET_BELT_ANGLE(self, gcmd):
        angle = gcmd.get_float("ANGLE", above=5.0, below=175.0)
        direction = gcmd.get_int(
            "DIRECTION", self.belt_direction, minval=-1, maxval=1
        )
        if direction == 0:
            raise gcmd.error("DIRECTION must be +1 or -1")
        save = gcmd.get_int("SAVE", 0, minval=0, maxval=1)
        self.belt_angle = angle
        self.belt_direction = direction
        self._update_trig()
        gcode_move = self.printer.lookup_object("gcode_move")
        gcode_move.reset_last_position()
        if save:
            configfile = self.printer.lookup_object("configfile")
            configfile.set("belt_translation", "belt_angle", "%.6f" % angle)
            configfile.set(
                "belt_translation",
                "belt_direction",
                "+1" if direction > 0 else "-1",
            )
            gcmd.respond_info(
                "Belt angle set to %.3f deg, direction %+d "
                "(staged; run SAVE_CONFIG to persist)"
                % (angle, direction)
            )
        else:
            gcmd.respond_info(
                "Belt angle set to %.3f deg, direction %+d (this session only)"
                % (angle, direction)
            )

    cmd_GET_BELT_ANGLE_help = "Report current belt angle and direction"

    def cmd_GET_BELT_ANGLE(self, gcmd):
        gcmd.respond_info(
            "belt_angle: %.4f deg, direction: %+d"
            % (self.belt_angle, self.belt_direction)
        )

    def get_status(self, eventtime):
        return {
            "belt_angle": self.belt_angle,
            "belt_direction": self.belt_direction,
        }


def load_config(config):
    return BeltTranslation(config)
