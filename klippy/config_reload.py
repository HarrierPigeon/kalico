# Apply edits to the config file to the running printer (RELOAD_CONFIG)
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""
Hot-loading of config values.

RELOAD_CONFIG re-reads the config file, compares it with the config the
printer is running with, and hands each changed option to the module that
reads it. A module opts options in with ConfigWrapper.register_reload_handler:

    config.register_reload_handler(self._reload, ("max_accel",),
                                   mid_print_safe=True)

    def _reload(self, config, changed):
        max_accel = config.getfloat("max_accel", above=0.0)  # check
        def apply():
            self.set_accel(max_accel)                       # apply
        return apply

The callback is the check step. It gets a ConfigWrapper for the section in the
new file and the set of changed options it claimed (a ReloadRequest), parses
and validates the new values, and returns a function that applies them (or
None if there is nothing to do). Every check runs before anything is applied,
so one bad value leaves the printer untouched. The callback may raise:
  - config errors, which reject the whole reload;
  - NeedsRestart, to say these changes can't be applied live after all;
  - UnsafeMidPrint, to refuse changes during a print (FORCE=1 overrides).

Options nobody claims, new or removed sections (unless a section factory is
registered for that prefix), and anything built into the MCU config need a
RESTART. Only options whose text changed since the file was last loaded are
applied, so values changed live with other commands stay as they are unless
that option was edited.
"""

import configparser
import fnmatch
import logging

error = configparser.Error

APPLY = "apply"
BLOCKED = "blocked"
RESTART = "restart"
FAILED = "failed"

MAX_VALUE_DISPLAY = 40
# Changes named in the dialog's one-line summary before "and N more"
PROMPT_MAX_NAMES = 3
# Sections that are only read through ${...} interpolation; their dependents
# show up in the comparison with their interpolated values instead.
IGNORED_SECTIONS = ("constants",)


class NeedsRestart(Exception):
    """Raised by a reload callback when its changes can't be applied live."""


class UnsafeMidPrint(Exception):
    """Raised by a reload callback to refuse a change during a print."""


class ReloadRequest(frozenset):
    """The changed options handed to a reload callback.

    It is a frozenset of option names, plus `mid_print`, which is True when a
    print is running and FORCE=1 was not given. Callbacks registered with
    mid_print_safe=True can check it to refuse only some of their changes.
    """

    def __new__(cls, options, mid_print=False):
        self = super().__new__(cls, options)
        self.mid_print = mid_print
        return self


def follows_edit(config, changed, option, default_from=None):
    """True if a handler should re-apply `option`: it was edited, or it is
    not set in the file and `default_from`, the option its default is
    derived from, was edited."""
    if option in changed:
        return True
    return (
        default_from is not None
        and default_from in changed
        and config.get(option, None, note_valid=False) is None
    )


class _Handler:
    def __init__(self, section, options, callback, mid_print_safe):
        self.section = section
        if isinstance(options, str):
            options = (options,)
        if not callable(options):
            options = tuple(o.lower() for o in options)
        self.options = options
        self.callback = callback
        self.mid_print_safe = mid_print_safe

    def claims(self, option):
        if callable(self.options):
            return bool(self.options(option))
        return any(fnmatch.fnmatchcase(option, p) for p in self.options)


class _Factory:
    def __init__(self, prefix, callback, mid_print_safe):
        self.prefix = prefix
        self.callback = callback
        self.mid_print_safe = mid_print_safe

    def matches(self, section):
        parts = section.split()
        return len(parts) > 1 and parts[0] == self.prefix


class Change:
    def __init__(self, section, option, old, new, status=RESTART, reason=""):
        self.section = section
        self.option = option
        self.old = old
        self.new = new
        self.status = status
        self.reason = reason
        self.applied = False

    def key(self):
        return (self.section, self.option, self.old, self.new, self.status)

    def describe(self):
        if self.option is None:
            what = "(new section)" if self.old is None else "(section removed)"
            text = "[%s] %s" % (self.section, what)
        elif self.new is None:
            text = "[%s] %s: (removed)" % (self.section, self.option)
        elif _is_long(self.new) or (
            self.old is not None and _is_long(self.old)
        ):
            what = "(added)" if self.old is None else "(changed)"
            text = "[%s] %s: %s" % (self.section, self.option, what)
        elif self.old is None:
            text = "[%s] %s: (added) %s" % (self.section, self.option, self.new)
        else:
            text = "[%s] %s: %s -> %s" % (
                self.section,
                self.option,
                self.old,
                self.new,
            )
        if self.reason:
            text += " (%s)" % (self.reason,)
        return text


def _is_long(value):
    return "\n" in value or len(value) > MAX_VALUE_DISPLAY


class _Call:
    def __init__(self, callback, section, changes, handler=None):
        self.callback = callback
        self.section = section
        self.changes = changes
        # None for a section factory, which owns every option of its section
        self.handler = handler
        self.apply_fn = None
        # (section, option) -> value the callback read, for get_status()
        self.tracking = {}


class ReloadPlan:
    def __init__(self, new_config, mid_print):
        self.new_config = new_config
        self.mid_print = mid_print
        self.changes = []
        self.calls = []
        self.errors = []

    def signature(self):
        return tuple(c.key() for c in self.changes)

    def by_status(self, status):
        return [c for c in self.changes if c.status == status]

    def leftovers(self):
        return [c for c in self.changes if c.status in (RESTART, BLOCKED)]


def snapshot(fileconfig):
    """Return {section: {option: value}}, with ${...} interpolated."""
    out = {}
    for section in fileconfig.sections():
        if section.lower() in IGNORED_SECTIONS:
            continue
        opts = {}
        for option in fileconfig.options(section):
            try:
                opts[option] = fileconfig.get(section, option)
            except error:
                opts[option] = fileconfig.get(section, option, raw=True)
        out[section] = opts
    return out


class ConfigReloader:
    def __init__(self, pconfig):
        self.pconfig = pconfig
        self.printer = pconfig.get_printer()
        self.handlers = []
        self.factories = []
        # What the running printer was built from (plus applied reloads).
        # PrinterConfig sets it when it parses the file, before any module
        # can add to the running config (Shake&Tune injects macro sections)
        self.baseline = None
        self.staged = None
        # {section: {option: new value or None}} still waiting for a restart
        self.pending = {}
        # A host prompt dialog (action:prompt_*) is showing the staged plan
        self.prompt_open = False

    # Registration
    def register_handler(self, section, options, callback, mid_print_safe):
        self.handlers.append(
            _Handler(section, options, callback, mid_print_safe)
        )

    def register_factory(self, prefix, callback, mid_print_safe):
        self.factories.append(_Factory(prefix, callback, mid_print_safe))

    # Printer state
    def _is_printing(self):
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is None:
            return False
        eventtime = self.printer.get_reactor().monotonic()
        state = print_stats.get_status(eventtime).get("state")
        return state in ("printing", "paused")

    def set_baseline(self, values):
        self.baseline = values
        self.staged = None
        self.pending = {}

    def _get_baseline(self):
        if self.baseline is None:
            raise error("RELOAD_CONFIG: the running config is unknown")
        return self.baseline

    def note_saved(self, saved):
        """SAVE_CONFIG RESTART=0 wrote values that are already live.

        `saved` is {section: {option: value}} or {section: None} for a
        removed section, as in PrinterConfig.status_save_pending.
        """
        baseline = self._get_baseline()
        for section, options in saved.items():
            if options is None:
                baseline.pop(section, None)
                continue
            baseline.setdefault(section, {}).update(options)

    # Planning
    def build_plan(self, force=False, section_filter=None):
        new_config = self.pconfig.read_new_config()
        old_values = self._get_baseline()
        new_values = snapshot(new_config.fileconfig)
        mid_print = self._is_printing() and not force
        plan = ReloadPlan(new_config, mid_print)
        sections = list(new_values)
        sections += [s for s in old_values if s not in new_values]
        for section in sections:
            if section_filter is not None and not section_filter(section):
                continue
            old = old_values.get(section)
            new = new_values.get(section)
            if old == new:
                continue
            if old is None:
                self._plan_new_section(plan, section, new)
            elif new is None:
                self._plan_removed_section(plan, section, old)
            else:
                self._plan_section(plan, section, old, new)
        for call in plan.calls:
            self._check_call(plan, call)
        return plan

    def _plan_removed_section(self, plan, section, old):
        if not old:
            plan.changes.append(Change(section, None, "", None))
        for option, value in old.items():
            plan.changes.append(
                Change(section, option, value, None, reason="section removed")
            )

    def _plan_new_section(self, plan, section, new):
        changes = [Change(section, o, None, v) for o, v in new.items()]
        if not changes:
            changes = [Change(section, None, None, "")]
        plan.changes.extend(changes)
        factory = next(
            (f for f in self.factories if f.matches(section)), None
        )
        if factory is None:
            for c in changes:
                c.reason = "new section"
            return
        status = APPLY
        if plan.mid_print and not factory.mid_print_safe:
            status = BLOCKED
        for c in changes:
            c.status = status
        if status == APPLY:
            plan.calls.append(
                _Call(factory.callback, section, changes)
            )

    def _plan_section(self, plan, section, old, new):
        handlers = [h for h in self.handlers if h.section == section]
        options = list(new) + [o for o in old if o not in new]
        calls = {}
        for option in options:
            if old.get(option) == new.get(option):
                continue
            change = Change(section, option, old.get(option), new.get(option))
            plan.changes.append(change)
            claimers = [h for h in handlers if h.claims(option)]
            if not claimers:
                continue
            if plan.mid_print and any(not h.mid_print_safe for h in claimers):
                change.status = BLOCKED
                continue
            change.status = APPLY
            for h in claimers:
                if h not in calls:
                    calls[h] = _Call(h.callback, section, [], handler=h)
                    plan.calls.append(calls[h])
                calls[h].changes.append(change)

    def _check_call(self, plan, call):
        # A wrapper of its own, so only what this callback read is recorded
        new_config = plan.new_config
        section_config = type(new_config)(
            self.printer, new_config.fileconfig, call.tracking, call.section
        )
        request = ReloadRequest(
            [c.option for c in call.changes if c.option is not None],
            plan.mid_print,
        )
        try:
            call.apply_fn = call.callback(section_config, request)
        except NeedsRestart as e:
            self._reclassify(call, RESTART, str(e))
        except UnsafeMidPrint as e:
            self._reclassify(call, BLOCKED, str(e))
        except (error, self.printer.command_error) as e:
            plan.errors.append(str(e))
        except Exception as e:
            logging.exception("RELOAD_CONFIG: checking [%s]", call.section)
            plan.errors.append(
                "Internal error checking [%s]: %s" % (call.section, e)
            )

    def _reclassify(self, call, status, reason):
        call.apply_fn = None
        for c in call.changes:
            if c.status == APPLY:
                c.status = status
                c.reason = reason
        call.changes = []

    # Applying
    def apply_plan(self, plan, file_edit=True):
        """Apply the changes in `plan`. `file_edit` is False for SET_CONFIG,
        where the file did not change: the baseline and the raw config in
        get_status() stay as they are."""
        failures = []
        applied = []
        tracking = {}
        for call in plan.calls:
            changes = [c for c in call.changes if c.status == APPLY]
            if not changes:
                continue
            try:
                if call.apply_fn is not None:
                    call.apply_fn()
            except Exception as e:
                logging.exception("RELOAD_CONFIG: applying [%s]", call.section)
                for c in changes:
                    c.status = FAILED
                    c.reason = str(e)
                failures.append("[%s]: %s" % (call.section, e))
                continue
            applied.extend(changes)
            # Only what this handler owns: it may also read options that
            # are still waiting for a restart
            for (section, option), value in call.tracking.items():
                if call.handler is None or call.handler.claims(option):
                    tracking[(section, option)] = value
        for c in applied:
            c.applied = True
        if not file_edit:
            self.pconfig.note_reloaded(applied, tracking, raw=False)
            return failures
        baseline = self._get_baseline()
        for c in plan.changes:
            if c.status != APPLY:
                continue
            opts = baseline.setdefault(c.section, {})
            if c.option is None:
                continue
            if c.new is None:
                opts.pop(c.option, None)
            else:
                opts[c.option] = c.new
        self.pconfig.note_reloaded(applied, tracking)
        return failures

    # Pending status
    def _update_pending(self, plan, section_filter):
        """Note which edits in the file are not live yet."""
        pending = {}
        if section_filter is not None:
            pending = {
                s: o for s, o in self.pending.items() if not section_filter(s)
            }
        for c in plan.changes:
            if c.applied:
                continue
            opts = pending.setdefault(c.section, {})
            if c.option is not None:
                opts[c.option] = c.new
        self.pending = pending

    # Report
    def _report(self, plan, header, applied=False):
        lines = [header]
        groups = [
            (APPLY, "Applied:" if applied else "Can apply now:"),
            (FAILED, "Failed to apply:"),
            (
                BLOCKED,
                "Not applied while printing (FORCE=1 to apply anyway):",
            ),
            (RESTART, "Needs RESTART:"),
        ]
        for status, title in groups:
            changes = plan.by_status(status)
            if not changes:
                continue
            lines.append(title)
            lines.extend("- " + c.describe() for c in changes)
        return "\n".join(lines)

    # SET_CONFIG
    def run_set(self, gcmd):
        section = gcmd.get("SECTION")
        option = gcmd.get("OPTION").lower()
        value = gcmd.get("VALUE")
        save = gcmd.get_int("SAVE", 0, minval=0, maxval=1)
        force = gcmd.get_int("FORCE", 0, minval=0, maxval=1)
        baseline = self._get_baseline()
        matches = [s for s in baseline if s.lower() == section.lower()]
        if not matches:
            raise gcmd.error("SET_CONFIG: unknown section '%s'" % (section,))
        section = matches[0]
        # The running values with this one option changed
        fileconfig = configparser.RawConfigParser(
            strict=False, interpolation=None
        )
        for name, options in baseline.items():
            fileconfig.add_section(name)
            for opt, val in options.items():
                fileconfig.set(name, opt, val)
        fileconfig.set(section, option, value)
        running = self.pconfig.get_running_config()
        new_config = type(running)(self.printer, fileconfig, {}, "printer")
        plan = ReloadPlan(new_config, self._is_printing() and not force)
        new = dict(baseline[section])
        new[option] = value
        self._plan_section(plan, section, baseline[section], new)
        what = "[%s] %s" % (section, option)
        if not plan.changes:
            gcmd.respond_info("SET_CONFIG: %s is already %s" % (what, value))
            return
        change = plan.changes[0]
        for call in plan.calls:
            self._check_call(plan, call)
        if plan.errors:
            raise gcmd.error("SET_CONFIG: %s" % ("\n".join(plan.errors),))
        reason = " (%s)" % (change.reason,) if change.reason else ""
        if change.status == RESTART:
            raise gcmd.error(
                "SET_CONFIG: %s can't be changed without a restart%s"
                % (what, reason)
            )
        if change.status == BLOCKED:
            raise gcmd.error(
                "SET_CONFIG: %s is not changed during a print%s;"
                " use FORCE=1 to change it anyway" % (what, reason)
            )
        failures = self.apply_plan(plan, file_edit=False)
        if failures:
            raise gcmd.error("SET_CONFIG: %s" % ("\n".join(failures),))
        msg = "SET_CONFIG: %s = %s" % (what, value)
        if save:
            self.pconfig.set(section, option, value)
            msg += "\nRun SAVE_CONFIG to write it to the config file."
        gcmd.respond_info(msg)

    # Dialog for the warning (Klipper's host prompts: Mainsail, Fluidd and
    # KlipperScreen show them). KlipperScreen keeps only the last
    # prompt_text and every action must be its own response.
    def _show_prompt(self, gcmd, name, plan):
        applicable = plan.by_status(APPLY)
        parts = ["%d edit(s) can apply now" % (len(applicable),)]
        for status, what in (
            (RESTART, "need a RESTART"),
            (BLOCKED, "not applied during a print"),
        ):
            changes = plan.by_status(status)
            if not changes:
                continue
            names = [
                "[%s] %s" % (c.section, c.option)
                if c.option
                else "[%s]" % (c.section,)
                for c in changes
            ]
            if len(names) > PROMPT_MAX_NAMES:
                extra = len(names) - PROMPT_MAX_NAMES
                names = names[:PROMPT_MAX_NAMES] + ["%d more" % (extra,)]
            parts.append("%d %s (%s)" % (len(changes), what, ", ".join(names)))
        text = "; ".join(parts) + ". Details are in the console."
        for line in (
            "prompt_begin Apply config edits?",
            "prompt_text " + text.replace("|", "/"),
            "prompt_footer_button Cancel|%s ABORT=1|secondary" % (name,),
            "prompt_footer_button Apply %d now|%s ACCEPT=1|primary"
            % (len(applicable), name),
            "prompt_show",
        ):
            gcmd.respond_raw("// action:" + line)
        self.prompt_open = True

    def _end_prompt(self, gcmd, force=False):
        if self.prompt_open or force:
            gcmd.respond_raw("// action:prompt_end")
        self.prompt_open = False

    # Command
    def run(
        self,
        gcmd,
        name="RELOAD_CONFIG",
        section_filter=None,
        allow_stage=True,
    ):
        if gcmd.get_int("ABORT", 0, minval=0, maxval=1):
            self._end_prompt(gcmd, force=True)
            if self.staged is None:
                gcmd.respond_info("%s: nothing to cancel" % (name,))
            else:
                self.staged = None
                gcmd.respond_info("%s: cancelled; nothing applied" % (name,))
            return
        accept = gcmd.get_int("ACCEPT", 0, minval=0, maxval=1)
        dry_run = gcmd.get_int("DRY_RUN", 0, minval=0, maxval=1)
        partial = gcmd.get_int("PARTIAL", 0, minval=0, maxval=1)
        force = gcmd.get_int("FORCE", 0, minval=0, maxval=1)
        prompt = gcmd.get_int("PROMPT", 1, minval=0, maxval=1)
        staged = None
        if accept:
            # Whatever happens next, the dialog is done with
            self._end_prompt(gcmd, force=True)
            staged, self.staged = self.staged, None
            if staged is None or staged[0] != name:
                raise gcmd.error(
                    "%s: nothing is waiting for ACCEPT; run %s first"
                    % (name, name)
                )
            force = force or staged[2]
        try:
            plan = self.build_plan(force, section_filter)
        except error as e:
            raise gcmd.error("%s: unable to read config: %s" % (name, e))
        if plan.errors:
            raise gcmd.error(
                "%s: nothing applied; the new config has errors:\n%s"
                % (name, "\n".join(plan.errors))
            )
        if staged is not None:
            if plan.signature() != staged[1]:
                raise gcmd.error(
                    "%s: the config file or the printer's state changed"
                    " since the warning; nothing applied. Run %s again."
                    % (name, name)
                )
            partial = True
        if not dry_run:
            # A new run replaces any plan still waiting for ACCEPT
            self.staged = None
            self._end_prompt(gcmd)
        self._update_pending(plan, section_filter)
        if not plan.changes:
            gcmd.respond_info("%s: no changes" % (name,))
            return
        applicable = plan.by_status(APPLY)
        leftovers = plan.leftovers()
        if dry_run:
            gcmd.respond_info(
                self._report(plan, "%s (dry run): nothing applied" % (name,))
            )
            return
        if leftovers and applicable and not partial and allow_stage:
            self.staged = (name, plan.signature(), force)
            msg = self._report(
                plan,
                "%s: some changes can't be applied now; nothing applied yet"
                % (name,),
            )
            msg += (
                "\nRun %s ACCEPT=1 to apply the %d change(s) that can apply"
                " now, or %s ABORT=1 to cancel." % (name, len(applicable), name)
            )
            gcmd.respond_info(msg)
            if prompt:
                self._show_prompt(gcmd, name, plan)
            return
        failures = self.apply_plan(plan) if applicable else []
        self._update_pending(plan, section_filter)
        header = "%s: applied %d change(s)" % (
            name,
            len(plan.by_status(APPLY)),
        )
        if not applicable:
            header = "%s: nothing can be applied now" % (name,)
        msg = self._report(plan, header, applied=True)
        if failures:
            raise gcmd.error(msg)
        gcmd.respond_info(msg)
