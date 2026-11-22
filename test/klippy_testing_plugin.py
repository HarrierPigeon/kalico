import ast
import logging
import pathlib
import shutil

from klippy.extras import gcode_macro


class KlippyTestingPlugin:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode_macro = self.printer.load_object(config, "gcode_macro")

        self.printer.register_event_handler(
            "gcode:command_error", self._command_error
        )
        self.printer.register_event_handler(
            "gcode:unknown_command", self._unknown_command
        )

        self.gcode.register_command("ASSERT", self.cmd_ASSERT)
        self.gcode.register_command("ASSERT_ERROR", self.cmd_ASSERT_ERROR)
        self.gcode.register_command("TEST_COPY_FILE", self.cmd_TEST_COPY_FILE)
        self.gcode.register_command("ASSERT_PY", self.cmd_ASSERT_PY)
        self.gcode.register_command("TEST_PY", self.cmd_TEST_PY)
        self.gcode.register_command(
            "TEST_INTERACTIVE", self.cmd_TEST_INTERACTIVE, lockfree=True
        )

    def _command_error(self):
        self.printer.request_exit("error_exit")
        self.printer.invoke_shutdown("Exception during testing")

    def _unknown_command(self, cmd):
        logging.error(f"Unknown command during test execution: {cmd}")
        self.printer.request_exit("error_exit")
        self.printer.invoke_shutdown(
            f"Unknown command during test execution: {cmd}"
        )

    def cmd_ASSERT_ERROR(self, gcmd):
        "Run one GCODE command and fail unless it raises (containing MATCH)"
        cmdline = gcmd.get("GCODE").strip()
        match = gcmd.get("MATCH", None)
        cmd = cmdline.split()[0].upper()
        handler = self.gcode.gcode_handlers.get(cmd)
        if handler is None:
            raise gcmd.error(f"ASSERT_ERROR: unknown command '{cmd}'")
        # Call the handler directly: in batch mode any error that reaches
        # the G-code dispatcher ends the run
        target = self.gcode.create_gcode_command(cmd, cmdline, {})
        try:
            handler(target)
        except self.gcode.error as e:
            if match is not None and match not in str(e):
                raise gcmd.error(
                    f"ASSERT_ERROR: '{cmdline}' failed without '{match}': {e}"
                )
            return
        raise gcmd.error(f"ASSERT_ERROR: '{cmdline}' did not fail")

    def cmd_ASSERT_PY(self, gcmd):
        "Evaluate a Python expression with `printer`; fail if it is falsy"
        expression = gcmd.get("TEST")
        value = eval(expression, {"printer": self.printer})
        if not value:
            raise gcmd.error(f"ASSERT_PY: {expression} == {value!r}")

    def cmd_TEST_PY(self, gcmd):
        "Run Python statements with `printer` (test setup only)"
        exec(gcmd.get("CODE"), {"printer": self.printer})

    def cmd_TEST_COPY_FILE(self, gcmd):
        "Copy SRC over DEST, both relative to the main config file's folder"
        config_dir = pathlib.Path(
            self.printer.get_start_args()["config_file"]
        ).parent.resolve()
        src = (config_dir / gcmd.get("SRC")).resolve()
        dest = (config_dir / gcmd.get("DEST")).resolve()
        for path in (src, dest):
            if config_dir not in path.parents:
                raise gcmd.error(
                    f"TEST_COPY_FILE: {path} is outside {config_dir}"
                )
        shutil.copyfile(src, dest)

    def cmd_TEST_INTERACTIVE(self, gcmd):
        """Submit one interactive input without holding the G-code mutex."""
        self.gcode.run_interactive_script(gcmd.get("GCODE"))

    def cmd_ASSERT(self, gcmd):
        "Evaluate an expression, raising an error if the return value is False"
        expression = gcmd.get("TEST")

        try:
            template = gcode_macro.Template(
                self.printer,
                self.gcode_macro.env,
                "ASSERT:runtime_expression",
                expression,
            )
        except:
            raise gcmd.error(f"ASSERT: Failed to parse '{expression}'")

        context = self.gcode_macro.create_template_context()
        statement = template.render(context)
        value = ast.literal_eval(statement) if statement else None

        if not value:
            raise gcmd.error(f"ASSERT: {expression} == {value}")


def load_config(config):
    return KlippyTestingPlugin(config)
