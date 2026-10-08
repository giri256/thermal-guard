#!/usr/bin/env python3
"""Flight recorder for hard freezes: every line is fsync'd, so the last
lines on disk show what the machine was doing when it froze. Run as root:
    sudo flight-recorder.py <out_dir>      (stop with Ctrl-C or SIGTERM)
Writes into <out_dir>:
    kmsg.log      every kernel message (all levels, independent of console loglevel)
    telemetry.log 4 Hz: GPU act/cur MHz, RC6 ms, temps, fans, RAPL watts, CPU MHz, mem, load
    gpu-top.log   intel_gpu_top JSON (engine busy %, freq, IRQs, RC6) every 250 ms
    engines.log   debugfs i915_engine_info + i915_frequency_info once a second
"""
import glob, os, signal, subprocess, sys, threading, time

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
CARD = sorted(glob.glob("/sys/class/drm/card[0-9]*/gt_max_freq_mhz"))[0].rsplit("/", 1)[0]
DBG = "/sys/kernel/debug/dri/" + CARD.split("card")[-1]
stop = threading.Event()


class Sink:
    def __init__(self, name):
        self.f = open(os.path.join(OUT, name), "a", buffering=1)

    def write(self, line):
        self.f.write(f"{time.strftime('%H:%M:%S', time.gmtime())}.{int(time.time()*1000)%1000:03d} {line.rstrip()}\n")
        self.f.flush()
        os.fsync(self.f.fileno())


def rd(path, default="?"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def kmsg():
    s = Sink("kmsg.log")
    fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
    os.lseek(fd, 0, os.SEEK_END)  # only new messages; the boot log is in the journal
    while not stop.is_set():
        try:
            rec = os.read(fd, 8192).decode(errors="replace")
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError as e:  # EPIPE = messages overwritten before we read them
            s.write(f"[kmsg read error: {e}]")
            continue
        head, _, msg = rec.partition(";")
        prio = head.split(",")[0]
        s.write(f"<{int(prio) & 7}> {msg.splitlines()[0] if msg else ''}")


def telemetry():
    s = Sink("telemetry.log")
    hw = {rd(h + "/name"): h for h in glob.glob("/sys/class/hwmon/hwmon*")}
    rapl = {rd(d + "/name"): d + "/energy_uj" for d in glob.glob("/sys/class/powercap/intel-rapl:*")}
    prev = {k: (time.monotonic(), int(rd(p, "0"))) for k, p in rapl.items()}
    s.write("cols: gpu_act/cur/max MHz | rc6_ms | temps C | fans rpm | RAPL W | cpu MHz avg/max | MemAvail MB | load")
    while not stop.is_set():
        g = f"gpu {rd(CARD+'/gt_act_freq_mhz')}/{rd(CARD+'/gt_cur_freq_mhz')}/{rd(CARD+'/gt_max_freq_mhz')}"
        rc6 = rd(CARD + "/power/rc6_residency_ms")
        temps = []
        for name in ("coretemp", "acpitz", "dell_smm"):
            for t in sorted(glob.glob(hw.get(name, "/x") + "/temp*_input")):
                temps.append(f"{name[:4]}{t.split('temp')[-1].split('_')[0]}={int(rd(t,'0'))//1000}")
        fans = [rd(f) for f in sorted(glob.glob(hw.get("dell_smm", "/x") + "/fan*_input"))]
        watts = []
        now = time.monotonic()
        for k, p in rapl.items():
            e = int(rd(p, "0"))
            t0, e0 = prev[k]
            if e >= e0 and now > t0:
                watts.append(f"{k}={(e-e0)/1e6/(now-t0):.1f}")
            prev[k] = (now, e)
        mhz = [float(l.split(":")[1]) for l in rd("/proc/cpuinfo", "").splitlines() if l.startswith("cpu MHz")]
        mem = next((l.split()[1] for l in rd("/proc/meminfo", "").splitlines() if l.startswith("MemAvailable")), "0")
        s.write(f"{g} | rc6 {rc6} | {' '.join(temps)} | fan {' '.join(fans)} | {' '.join(watts)} | "
                f"cpu {sum(mhz)/max(len(mhz),1):.0f}/{max(mhz, default=0):.0f} | mem {int(mem)//1024} | "
                f"load {rd('/proc/loadavg').split(' ')[0]}")
        stop.wait(0.25)


def gputop():
    s = Sink("gpu-top.log")
    try:
        p = subprocess.Popen(["intel_gpu_top", "-J", "-s", "250"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        s.write(f"[intel_gpu_top failed: {e}]")
        return
    for line in p.stdout:
        if stop.is_set():
            break
        if line.strip():
            s.write(line)
    p.terminate()


def engines():
    s = Sink("engines.log")
    while not stop.is_set():
        for f in ("i915_engine_info", "i915_frequency_info"):
            s.write(f"--- {f}\n" + rd(f"{DBG}/{f}", "(unreadable)"))
        stop.wait(1.0)


signal.signal(signal.SIGTERM, lambda *a: stop.set())
signal.signal(signal.SIGINT, lambda *a: stop.set())
Sink("kmsg.log").write(f"[recorder start, card {CARD}, kernel {os.uname().release}]")
threads = [threading.Thread(target=f, daemon=True) for f in (kmsg, telemetry, gputop, engines)]
for t in threads:
    t.start()
while not stop.is_set():
    stop.wait(1)
Sink("kmsg.log").write("[recorder stop]")
