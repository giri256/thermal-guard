# Why Intel's thermald didn't save this laptop

thermald is the standard answer to "my Intel laptop runs hot on Linux", and it's a good
daemon. On this machine it let the laptop freeze at 98 °C without taking a single useful
action. These notes come from reading the **thermald 2.5.13** source next to a recorded crash.
They explain why, so you can tell whether your laptop is in the same position.

## What happened

Stock service (`thermald --adaptive`), same glmark2 test as everything else in this repo:

| | |
|---|---|
| Config | no DPTF/GDDV tables on this machine → `--adaptive` falls back to its built-in defaults |
| Crossed 96 °C | 23 s into the test |
| Froze | about 6 s later, at 98 °C |
| GPU clock at the freeze | 1250 MHz (maximum, never touched) |
| CPU clock at the freeze | 3190 MHz (full turbo, never touched) |
| thermald log | no cooling action |

## What the source says it does on a machine like this

**1. The trip point is 96 °C.**
Without a config, `thd_zone_cpu.cpp` puts the passive trip halfway between coretemp's
`max` and `crit`: `87 + (105 − 87) / 2 = 96 °C`. This laptop freezes around 98–104 °C,
so that leaves 2–8 degrees of headroom.

**2. Above ~86 °C it checks once every 4 seconds.**
A polling zone starts 10 % below the trip, and the poll interval is 4 s
(`thd_poll_interval` in `main.cpp`). This die can jump 9 °C in 0.3 s, so a
4-second cadence is too slow.

**3. Cooling devices are tried strictly in order.**
The default zone drives them one after another (`SEQUENTIAL`), one step per action, with a
2–4 s debounce between steps:

```
rapl_controller → intel_pstate → intel_powerclamp → cpufreq → Processor
```

**4. The first lever is locked on this laptop.**
The BIOS locks the RAPL power limit (PL1 35 W, and the package only draws ~28 W anyway).
`thd_cdev_rapl.cpp` then marks the device "at max state" without changing anything, so the
first cooling cycle is spent doing nothing.

**5. The second lever is too gentle.**
`intel_pstate` lowers `max_perf_pct` in 10 % steps and only switches turbo off at ≤ 70 %.
That takes three or more debounced steps.

**6. Nothing touches the GPU.**
None of the default cooling devices change the i915 GPU clock, and on this machine the GPU
is what drives the temperature spikes. thermald's XML config can declare a generic sysfs
device (`thd_cdev_gen_sysfs.cpp`), but it writes one file. i915 needs `gt_boost_freq_mhz`
and `gt_max_freq_mhz` changed together, in the right order, so that wasn't pursued.

## What thermal-guard took from it

thermald gets several things right, and thermal-guard borrows them:

- **Remember and restore.** Level 0 means "whatever you had when the service started", and
  it is restored on exit. A clock limit you set yourself is respected.
- **Read back and re-apply.** If something else rewrites the clock or turbo setting while
  throttled, the guard puts its value back and logs it.
- **Devices can disappear.** The GPU is looked up again on every poll, so passing it through
  to a VM doesn't crash the daemon.

Deliberately left out: XML configuration, D-Bus, PID control, interrupt-driven trip points
and the generic cooling-device framework. A 0.5 s poll of a few sysfs files costs almost
nothing, and it reacts faster than any of those.

## When thermald *is* the right tool

If your laptop ships DPTF tables (most from ~2016 on), has an unlocked RAPL limit, or gets
hot from CPU load rather than GPU load, use thermald. thermal-guard is for the narrow case
where none of that holds: an older laptop whose firmware leaves the GPU running flat out
until the machine dies.
