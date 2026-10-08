#!/usr/bin/env python3
"""thermal-guard — a software thermal throttle for Intel laptops whose firmware won't throttle in time.

Polls the hottest coretemp sensor twice a second. When it gets hot, it steps the i915 GPU's
max clock down (and, with --turbo-control, switches CPU turbo off); when it cools, it steps
back up. Level 0 is whatever settings it found at start, and they are restored on exit.

    thermal-guard.py [--turbo-control] [--hot C] [--very-hot C] [--cool C] [--dry-run]

Defaults are derived from the CPU's critical temperature (crit - 13 / crit - 8 / crit - 20,
i.e. 92 / 97 / 85 °C on a 105 °C part). Needs root. Python 3 stdlib only.
"""
import argparse, glob, os, signal, sys, time

STEP_S, COOL_S, POLL_S = 1.0, 5.0, 0.5
# (GPU max as a fraction of its hardware maximum RP0, CPU turbo allowed?)
# Level 0 = the settings found at start. Clocks are rounded to 50 MHz and never below RPn.
LEVELS = [(None, True), (0.80, True), (0.68, False), (0.52, False), (0.40, False), (0.0, False)]


def read_int(path):
    with open(path) as f:
        return int(f.read())


def coretemp_sensors():
    out = []
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(h + "/name").read().strip() == "coretemp":
                out += glob.glob(h + "/temp*_input")
        except OSError:
            pass
    return out


def crit_temp(sensors):
    crits = []
    for p in sensors:
        try:
            crits.append(read_int(p.replace("_input", "_crit")) // 1000)
        except (OSError, ValueError):
            pass
    return min(crits) if crits else 100


def card():
    """i915 card dir, or None while there is none (e.g. the GPU is passed through to a VM)."""
    c = sorted(glob.glob("/sys/class/drm/card[0-9]*/gt_max_freq_mhz"))
    return c[0].rsplit("/", 1)[0] if c else None


class Turbo:
    """CPU turbo switch: intel_pstate's no_turbo (1 = off) or cpufreq's boost (0 = off)."""
    def __init__(self):
        for path, off in (("/sys/devices/system/cpu/intel_pstate/no_turbo", 1),
                          ("/sys/devices/system/cpu/cpufreq/boost", 0)):
            if os.path.exists(path):
                self.path, self.off = path, off
                return
        self.path = None

    def get(self):
        return read_int(self.path) if self.path else None


class Guard:
    def __init__(self, args):
        self.args, self.turbo = args, Turbo()
        self.sensors = coretemp_sensors()
        crit = crit_temp(self.sensors)
        self.hot = args.hot or crit - 13
        self.very_hot = args.very_hot or crit - 8
        self.cool = args.cool or crit - 20
        self.orig = {"gpu": self.gpu_now(card()), "turbo": self.turbo.get()}

    @staticmethod
    def gpu_now(c):
        return (read_int(c + "/gt_max_freq_mhz"), read_int(c + "/gt_boost_freq_mhz")) if c else None

    def write(self, path, val):
        if self.args.dry_run:
            return
        try:
            with open(path, "w") as f:
                f.write(str(val))
        except OSError as e:
            print(f"write {path}={val} failed: {e}", flush=True)

    def wanted(self, level, c):
        """(gpu max, gpu boost) or None, and the turbo value this level should have."""
        frac, turbo_ok = LEVELS[level]
        gpu = None
        if c:
            rp0, rpn = read_int(c + "/gt_RP0_freq_mhz"), read_int(c + "/gt_RPn_freq_mhz")
            full = self.orig["gpu"] or (rp0, rp0)
            if frac is None:
                gpu = full
            else:
                mhz = max(rpn, int(rp0 * frac) // 50 * 50)
                gpu = (min(mhz, full[0]), min(mhz, full[1]))
        tv = self.orig["turbo"]
        if self.args.turbo_control and not turbo_ok and self.turbo.path:
            tv = self.turbo.off
        return gpu, tv

    def apply(self, level):
        """Write whatever differs from what this level wants. Returns True if anything changed."""
        c = card()
        gpu, tv = self.wanted(level, c)
        changed = False
        if c and gpu:
            cur = self.gpu_now(c)
            if cur != gpu:
                # i915 rejects boost > max and max < boost: lower boost first, raise max first
                order = [("gt_boost_freq_mhz", gpu[1]), ("gt_max_freq_mhz", gpu[0])]
                for f, v in order if gpu[0] < cur[0] else order[::-1]:
                    self.write(f"{c}/{f}", v)
                changed = True
        if self.turbo.path and self.turbo.get() != tv:
            self.write(self.turbo.path, tv)
            changed = True
        return changed

    def temp(self):
        return max(read_int(p) for p in self.sensors) // 1000

    def describe(self, level):
        gpu, tv = self.wanted(level, card())
        turbo = "-" if tv is None else ("off" if tv == self.turbo.off else "on")
        return f"GPU max {gpu[0] if gpu else '-'} MHz, turbo {turbo}"

    def run(self):
        if not self.sensors:
            sys.exit("thermal-guard: no coretemp sensors (is the coretemp module loaded?)")
        stop = []
        signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
        signal.signal(signal.SIGINT, lambda *a: stop.append(1))
        print(f"thermal-guard: GPU {card() or 'none'}, {len(self.sensors)} sensors, "
              f"hot {self.hot} / very hot {self.very_hot} / cool {self.cool} °C, "
              f"turbo control {'on' if self.args.turbo_control else 'off'}"
              f"{', DRY RUN' if self.args.dry_run else ''}", flush=True)
        level, last_step, cool_since, had_card = 0, 0.0, None, card() is not None
        top = len(LEVELS) - 1
        while not stop:
            t, now, new = self.temp(), time.monotonic(), level
            if t >= self.very_hot:
                new = min(level + 2, top)
            elif t >= self.hot and now - last_step >= STEP_S:
                new = min(level + 1, top)
            if t <= self.cool and level > 0:
                cool_since = cool_since or now
                if now - cool_since >= COOL_S:
                    new, cool_since = level - 1, now
            elif t > self.cool:
                cool_since = None
            has_card = card() is not None
            if has_card != had_card:
                print(f"GPU {'back: ' + card() if has_card else 'gone (passed through to a VM?)'}", flush=True)
                if has_card and self.orig["gpu"] is None:
                    self.orig["gpu"] = self.gpu_now(card())
                had_card = has_card
            if new != level:
                self.apply(new)
                print(f"{t} °C: level {level} -> {new} ({self.describe(new)})", flush=True)
                level, last_step = new, now
            elif level > 0 and self.apply(level):
                print(f"{t} °C: level {level} settings were overwritten, re-applied", flush=True)
            time.sleep(POLL_S)
        self.apply(0)
        print("thermal-guard: stopped, original settings restored", flush=True)


def main():
    p = argparse.ArgumentParser(description="Software thermal throttle for Intel iGPU laptops.")
    p.add_argument("--turbo-control", action="store_true",
                   help="also switch CPU turbo off from level 2 (safer; default leaves turbo alone)")
    p.add_argument("--hot", type=int, help="°C to step down one level per second (default crit-13)")
    p.add_argument("--very-hot", type=int, help="°C to step down two levels at once (default crit-8)")
    p.add_argument("--cool", type=int, help="°C to step back up after 5 s (default crit-20)")
    p.add_argument("--dry-run", action="store_true", help="log decisions, write nothing")
    Guard(p.parse_args()).run()


if __name__ == "__main__":
    main()
