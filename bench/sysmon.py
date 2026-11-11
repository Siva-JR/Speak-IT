"""Background system sampler: CPU, memory, frequency, temperature, GPU. Writes a time series."""
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil


def _read(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def _gpu_sources():
    """Detect available GPU telemetry. Returns a sampler callable or None."""
    if shutil.which("nvidia-smi"):
        def nv():
            try:
                o = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=1).stdout.strip()
                u, m, p, t = [float(x) for x in o.split("\n")[0].split(",")]
                return {"gpu_util": u, "gpu_mem_mb": m, "gpu_power_w": p, "gpu_temp_c": t}
            except Exception:
                return {}
        return nv
    # Intel / AMD iGPU through sysfs: only frequency is reliably readable without root
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        for name in ("gt_cur_freq_mhz", "device/gt_cur_freq_mhz", "gt/gt0/rps_cur_freq_mhz"):
            p = card / name
            if p.exists():
                def igpu(p=p):
                    v = _read(p)
                    return {"gpu_freq_mhz": float(v)} if v else {}
                return igpu
        busy = card / "device/gpu_busy_percent"
        if busy.exists():
            def amd(p=busy):
                v = _read(p)
                return {"gpu_util": float(v)} if v else {}
            return amd
    return None


class SysMon:
    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self.rows = []
        self.events = []  # (t, label)
        self._stop = threading.Event()
        self._thread = None
        self._proc = psutil.Process(os.getpid())
        self._gpu = _gpu_sources()
        self.gpu_available = self._gpu is not None
        self.t0 = None

    def mark(self, label: str):
        self.events.append((time.perf_counter() - self.t0, label))

    def _temps(self):
        out = {}
        try:
            for name, entries in psutil.sensors_temperatures().items():
                for e in entries:
                    if name == "coretemp" and (e.label or "").startswith("Package"):
                        out["cpu_pkg_temp_c"] = e.current
                if "cpu_pkg_temp_c" not in out and name in ("coretemp", "k10temp", "acpitz") and entries:
                    out.setdefault("cpu_pkg_temp_c", entries[0].current)
        except Exception:
            pass
        return out

    def _loop(self):
        self._proc.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        n = 0
        while not self._stop.is_set():
            n += 1
            t = time.perf_counter() - self.t0
            per_core = psutil.cpu_percent(None, percpu=True)
            row = {
                "t": round(t, 3),
                "sys_cpu_pct": round(sum(per_core) / len(per_core), 1),
                "busiest_core_pct": max(per_core),
                "proc_cpu_pct": self._proc.cpu_percent(None),  # 100 = one full core
                "proc_rss_mb": round(self._proc.memory_info().rss / 1e6, 1),
                "proc_threads": self._proc.num_threads(),
                "sys_mem_used_mb": round(psutil.virtual_memory().used / 1e6, 1),
            }
            if n % 5 == 1:  # smaps walk is slower, sample it less often
                try:
                    m = self._proc.memory_full_info()
                    row["proc_pss_mb"] = round(m.pss / 1e6, 1)
                    row["proc_uss_mb"] = round(m.uss / 1e6, 1)
                except Exception:
                    pass
            f = psutil.cpu_freq()
            if f:
                row["cpu_freq_mhz"] = round(f.current, 0)
            row.update(self._temps())
            if self._gpu:
                row.update(self._gpu())
            self.rows.append(row)
            self._stop.wait(self.interval)

    def start(self):
        self.t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def summary(self):
        import pandas as pd
        if not self.rows:
            return {}
        df = pd.DataFrame(self.rows)
        s = {}
        for col, fn in [("sys_cpu_pct", "mean"), ("sys_cpu_pct", "max"), ("proc_cpu_pct", "mean"),
                        ("proc_cpu_pct", "max"), ("proc_rss_mb", "max"), ("proc_pss_mb", "max"), ("proc_uss_mb", "max"), ("cpu_freq_mhz", "mean"),
                        ("cpu_freq_mhz", "min"), ("cpu_pkg_temp_c", "max"), ("gpu_util", "mean"),
                        ("gpu_util", "max"), ("gpu_freq_mhz", "mean"), ("gpu_mem_mb", "max"),
                        ("gpu_power_w", "mean")]:
            if col in df:
                s[f"{col}_{fn}"] = round(float(getattr(df[col], fn)()), 2)
        return s
