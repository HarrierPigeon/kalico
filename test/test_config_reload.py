"""RELOAD_CONFIG: apply config file edits without a restart.

Each test runs a batch-mode klippy on a copy of test_configs/config_reload,
copies one of its tuning_*.cfg files over tuning.cfg mid-run, reloads, and
checks the live values. A failed ASSERT makes klippy exit with an error.
"""

import pathlib
import subprocess
import sys
import typing

import pytest

ROOT = pathlib.Path(__file__).parent.parent
DICTIONARY = "stm32f446.dict"
ConfigRoot = typing.Annotated[pathlib.Path, "test_configs/config_reload"]


@pytest.fixture
def run_gcode(request, config_root, tmp_path):
    dictionary = pathlib.Path.cwd() / request.config.getoption("dictdir")
    dictionary = dictionary / DICTIONARY

    def run(script):
        lines = [line.strip() for line in script.strip().splitlines()]
        lines = [line for line in lines if line and not line.startswith("#")]
        gcode_file = tmp_path / "_test_.gcode"
        gcode_file.write_text("\n".join(lines) + "\n")
        args = [
            sys.executable,
            "-m",
            "klippy",
            str(config_root / "printer.cfg"),
            "-i",
            str(gcode_file),
            "-o",
            str(tmp_path / "_test_.output"),
            "-v",
            "-d",
            str(dictionary),
        ]
        result = subprocess.run(
            args, cwd=ROOT, text=True, capture_output=True
        )
        if result.returncode != 0:
            pytest.fail(
                "klippy exited with %d\n%s"
                % (result.returncode, result.stderr[-6000:])
            )
        return result.stderr

    return run


def test_reload_applies_hot_values(config_root: ConfigRoot, run_gcode):
    run_gcode(
        """
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        TEST_COPY_FILE SRC=tuning_hot.cfg DEST=tuning.cfg
        RELOAD_CONFIG

        # Nothing is left over
        ASSERT TEST='{printer.configfile.reload_pending == {}}'

        # Toolhead and kinematics limits
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'
        ASSERT TEST='{printer.toolhead.square_corner_velocity == 8}'
        ASSERT TEST='{printer.toolhead.minimum_cruise_ratio == 0.25}'
        ASSERT TEST='{printer.toolhead.max_velocity == 300}'
        ASSERT_PY TEST='printer.lookup_object("toolhead").get_kinematics().max_z_velocity == 10'
        ASSERT_PY TEST='printer.lookup_object("toolhead").get_kinematics().max_z_accel == 100'
        # The reloaded values are also what RESET_VELOCITY_LIMIT returns to
        SET_VELOCITY_LIMIT ACCEL=1000
        RESET_VELOCITY_LIMIT
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'

        # get_status() of configfile follows
        ASSERT TEST='{printer.configfile.settings.printer.max_accel == 5000}'
        ASSERT TEST='{printer.configfile.config.printer.max_accel == "5000"}'

        # Input shaper: only the edited values change
        ASSERT_PY TEST='printer.lookup_object("input_shaper").shapers[0].params.shaper_type == "ei"'
        ASSERT_PY TEST='printer.lookup_object("input_shaper").shapers[0].params.shaper_freq == 52.5'
        ASSERT_PY TEST='printer.lookup_object("input_shaper").shapers[1].params.shaper_type == "mzv"'
        ASSERT_PY TEST='printer.lookup_object("input_shaper").shapers[1].params.shaper_freq == 40'
        ASSERT_PY TEST='printer.lookup_object("input_shaper").shapers[1].params.damping_ratio == 0.15'

        # TMC current and register field
        ASSERT TEST='{printer["tmc2209 stepper_x"].run_current > 0.75}'
        ASSERT_PY TEST='printer.lookup_object("tmc2209 stepper_x").fields.get_field("tbl") == 1'

        # Extruder
        ASSERT TEST='{printer.extruder.pressure_advance == 0.06}'
        ASSERT TEST='{printer.extruder.smooth_time == 0.03}'
        ASSERT_PY TEST='printer.lookup_object("extruder").extruder_stepper.stepper.get_rotation_distance()[0] == 22.6'

        # Heaters: PID gain in place, watermark rebuilt, smooth time, min temp
        ASSERT_PY TEST='abs(printer.lookup_object("extruder").get_heater().get_control().Kp * 255 - 30.5) < 1e-9'
        ASSERT_PY TEST='abs(printer.lookup_object("extruder").get_heater().get_control().Ki * 255 - 1.08) < 1e-9'
        ASSERT_PY TEST='printer.lookup_object("heater_bed").heater.get_control().max_delta == 3.0'
        ASSERT_PY TEST='printer.lookup_object("extruder").get_heater().get_smooth_time() == 2.0'
        ASSERT_PY TEST='printer.lookup_object("extruder").get_heater().min_extrude_temp == 180'

        # Macros: variables edited in the file change, others stay
        SET_GCODE_VARIABLE MACRO=TEST_MACRO VARIABLE=untouched VALUE="'live'"
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].count == 2}'
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].added == "new"}'
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].untouched == "live"}'
        ASSERT TEST='{printer.gcode.commands.TEST_MACRO.help == "Second version"}'
        TEST_MACRO
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].count == 11}'
        # A macro added to the file
        NEW_MACRO
        ASSERT TEST='{printer["gcode_macro NEW_MACRO"].ran == 1}'
        # delayed_gcode body
        ASSERT_PY TEST='"VALUE=21" in printer.lookup_object("delayed_gcode TEST_DELAYED").timer_gcode.render()'

        # A second reload with no further edits does nothing
        RELOAD_CONFIG
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'
        """
    )


def test_reload_mixed_edit_warns_then_accepts(
    config_root: ConfigRoot, run_gcode
):
    run_gcode(
        """
        TEST_COPY_FILE SRC=tuning_mixed.cfg DEST=tuning.cfg
        RELOAD_CONFIG
        # Warned and staged: nothing applied yet
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        ASSERT TEST='{printer.configfile.reload_pending.stepper_x.position_max == "210"}'
        ASSERT TEST='{printer.configfile.reload_pending.printer.max_accel == "5000"}'
        RELOAD_CONFIG ACCEPT=1
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'
        ASSERT TEST='{printer.configfile.reload_pending.stepper_x.position_max == "210"}'
        ASSERT TEST='{"printer" not in printer.configfile.reload_pending}'
        ASSERT TEST='{printer.configfile.warnings|selectattr("type", "eq", "reload_pending")|list|length == 1}'
        # Nothing is staged any more
        ASSERT_ERROR GCODE='RELOAD_CONFIG ACCEPT=1' MATCH='nothing is waiting'

        # ABORT drops a staged plan
        TEST_COPY_FILE SRC=tuning_mixed2.cfg DEST=tuning.cfg
        RELOAD_CONFIG
        RELOAD_CONFIG ABORT=1
        ASSERT_ERROR GCODE='RELOAD_CONFIG ACCEPT=1' MATCH='nothing is waiting'
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'

        # PARTIAL=1 applies what it can straight away
        RELOAD_CONFIG PARTIAL=1
        ASSERT TEST='{printer.toolhead.max_accel == 6000}'
        """
    )


def test_reload_accept_refuses_changed_file(config_root: ConfigRoot, run_gcode):
    run_gcode(
        """
        TEST_COPY_FILE SRC=tuning_mixed.cfg DEST=tuning.cfg
        RELOAD_CONFIG
        TEST_COPY_FILE SRC=tuning_mixed2.cfg DEST=tuning.cfg
        ASSERT_ERROR GCODE='RELOAD_CONFIG ACCEPT=1' MATCH='changed since the warning'
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        """
    )


def test_reload_during_print(config_root: ConfigRoot, run_gcode):
    run_gcode(
        """
        TEST_PY CODE='printer.lookup_object("print_stats").note_start()'
        TEST_COPY_FILE SRC=tuning_print.cfg DEST=tuning.cfg
        RELOAD_CONFIG
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        RELOAD_CONFIG ACCEPT=1
        # Velocity limits are safe mid-print; the driver field and macro
        # variable are not
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'
        ASSERT_PY TEST='printer.lookup_object("tmc2209 stepper_x").fields.get_field("tbl") == 2'
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].count == 1}'
        ASSERT TEST='{printer.configfile.reload_pending["tmc2209 stepper_x"].driver_tbl == "1"}'
        # FORCE=1 applies them anyway
        RELOAD_CONFIG FORCE=1
        ASSERT_PY TEST='printer.lookup_object("tmc2209 stepper_x").fields.get_field("tbl") == 1'
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].count == 3}'
        ASSERT TEST='{printer.configfile.reload_pending == {}}'
        """
    )


def test_reload_rejects_invalid_value(config_root: ConfigRoot, run_gcode):
    run_gcode(
        """
        TEST_COPY_FILE SRC=tuning_bad.cfg DEST=tuning.cfg
        ASSERT_ERROR GCODE='RELOAD_CONFIG' MATCH='must have minimum'
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        ASSERT_PY TEST='printer.lookup_object("input_shaper").shapers[0].params.shaper_freq == 50'
        """
    )


def test_reload_dry_run(config_root: ConfigRoot, run_gcode):
    run_gcode(
        """
        TEST_COPY_FILE SRC=tuning_mixed.cfg DEST=tuning.cfg
        RELOAD_CONFIG DRY_RUN=1
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        ASSERT_ERROR GCODE='RELOAD_CONFIG ACCEPT=1' MATCH='nothing is waiting'
        ASSERT TEST='{printer.configfile.reload_pending.stepper_x.position_max == "210"}'
        """
    )


def test_reload_gcode_macros_only_touches_macros(
    config_root: ConfigRoot, run_gcode
):
    run_gcode(
        """
        TEST_COPY_FILE SRC=tuning_macros.cfg DEST=tuning.cfg
        RELOAD_GCODE_MACROS
        TEST_MACRO
        ASSERT TEST='{printer["gcode_macro TEST_MACRO"].count == 12}'
        ASSERT TEST='{printer.toolhead.max_accel == 3000}'
        # The [printer] edit is still there for RELOAD_CONFIG
        RELOAD_CONFIG
        ASSERT TEST='{printer.toolhead.max_accel == 5000}'
        """
    )


def test_reload_follows_constants(config_root: ConfigRoot, run_gcode):
    run_gcode(
        """
        TEST_COPY_FILE SRC=tuning_constants.cfg DEST=tuning.cfg
        RELOAD_CONFIG
        ASSERT_PY TEST='printer.lookup_object("toolhead").get_kinematics().max_z_accel == 150'
        ASSERT TEST='{printer.configfile.reload_pending == {}}'
        """
    )


def test_reload_refused_handler_leaves_status(
    config_root: ConfigRoot, run_gcode
):
    run_gcode(
        """
        TEST_PY CODE='printer.lookup_object("print_stats").note_start()'
        TEST_COPY_FILE SRC=tuning_print_heater.cfg DEST=tuning.cfg
        RELOAD_CONFIG PARTIAL=1
        # Pressure advance applies; rebuilding the heater's controller doesn't
        ASSERT TEST='{printer.extruder.pressure_advance == 0.07}'
        ASSERT TEST='{printer.configfile.settings.extruder.pressure_advance == 0.07}'
        ASSERT_PY TEST='printer.lookup_object("extruder").get_heater().get_max_power() == 1.0'
        ASSERT TEST='{printer.configfile.settings.extruder.max_power == 1.0}'
        ASSERT TEST='{printer.configfile.reload_pending.extruder.max_power == "0.9"}'
        RELOAD_CONFIG FORCE=1
        ASSERT_PY TEST='printer.lookup_object("extruder").get_heater().get_max_power() == 0.9'
        ASSERT_PY TEST='printer.lookup_object("extruder").get_heater().get_control().heater_max_power == 0.9'
        ASSERT TEST='{printer.configfile.settings.extruder.max_power == 0.9}'
        ASSERT TEST='{printer.configfile.reload_pending == {}}'
        """
    )
