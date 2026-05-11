// Hardware emergency-stop input.
//
// Watches a single GPIO pin and triggers an MCU-side shutdown the moment
// the configured trigger condition is observed (with simple debouncing).
// Unlike the host-driven M112 path, this handler is entirely autonomous on
// the MCU: once the [hardware_estop] section issues the config command, the
// MCU continues polling regardless of whether the host is still alive.
//
// Wiring: industrial safety practice is a normally-closed (NC) contact wired
// to ground via a pull-up resistor. With invert=1 (pull_up=1), a healthy
// circuit reads logic 0 and any open (button pressed, cut wire, lost
// connector) reads logic 1 and trips the stop.
//
// Copyright (C) 2026  Kalico contributors
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_in_setup
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_from_us, timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // sched_add_timer

// Poll interval - 1ms gives sub-2ms worst-case trigger latency. The shutdown
// path itself takes microseconds once we call shutdown().
#define ESTOP_POLL_US 1000
// Number of consecutive matching samples required before tripping. With a
// 1ms poll rate this gives ~3ms debounce, more than enough for a hardware
// switch with mechanical bounce on the order of microseconds.
#define ESTOP_DEBOUNCE_COUNT 3

struct estop_input {
    struct timer time;
    struct gpio_in pin;
    uint32_t rest_ticks;
    uint8_t trigger_value; // pin level that means "tripped" (0 or 1)
    uint8_t match_count;   // consecutive matching samples seen so far
    uint8_t triggered;     // 0 until shutdown() is called - then 1
};

static uint_fast8_t
estop_event(struct timer *t)
{
    struct estop_input *e = container_of(t, struct estop_input, time);

    uint8_t val = !!gpio_in_read(e->pin);
    if (val == e->trigger_value) {
        e->match_count++;
        if (e->match_count >= ESTOP_DEBOUNCE_COUNT) {
            // Mark triggered before shutdown() longjmps out so a SHUTDOWN
            // handler (or status query) can see we are the cause.
            e->triggered = 1;
            shutdown("Emergency stop button pressed");
        }
    } else {
        e->match_count = 0;
    }

    e->time.waketime += e->rest_ticks;
    return SF_RESCHEDULE;
}

void
command_config_estop_input(uint32_t *args)
{
    struct estop_input *e = oid_alloc(
        args[0], command_config_estop_input, sizeof(*e));
    e->pin = gpio_in_setup(args[1], args[2]);
    e->trigger_value = !!args[3];
    e->match_count = 0;
    e->triggered = 0;
    e->rest_ticks = timer_from_us(ESTOP_POLL_US);
    e->time.func = estop_event;
    e->time.waketime = timer_read_time() + e->rest_ticks;
    sched_add_timer(&e->time);
}
DECL_COMMAND(command_config_estop_input,
             "config_estop_input oid=%c pin=%u pull_up=%c trigger_value=%c");

// Allow the host to query the current pin state (e.g. for status reporting
// or to verify wiring before the user arms the machine). Safe to call after
// shutdown - it does not change state.
void
command_query_estop_input(uint32_t *args)
{
    uint8_t oid = args[0];
    struct estop_input *e = oid_lookup(oid, command_config_estop_input);
    irq_disable();
    uint8_t triggered = e->triggered;
    uint8_t match = e->match_count;
    irq_enable();
    sendf("estop_input_state oid=%c pin_value=%c triggered=%c match_count=%c",
          oid, gpio_in_read(e->pin), triggered, match);
}
// Mark as runnable in shutdown so the host can still poll us after a trip
// to confirm the button is what caused the stop.
DECL_COMMAND_FLAGS(command_query_estop_input, HF_IN_SHUTDOWN,
                   "query_estop_input oid=%c");
