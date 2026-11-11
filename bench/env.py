"""Capture everything about the machine and software so runs can be compared later."""
import json
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import psutil


def _sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None


def _read(p):
    try:
        return Path(p).read_text().strip()
    except Exception:
        return None


def capture():
    pkgs = {}
    for p in ["faster-whisper", "ctranslate2", "pywhispercpp", "onnxruntime", "sherpa-onnx",
              "numpy", "jiwer", "psutil"]:
        try:
            pkgs[p] = metadata.version(p)
        except Exception:
            pass
    batt = psutil.sensors_battery()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "kernel": platform.release(),
        "cpu_model": _sh("lscpu | grep 'Model name' | sed 's/.*: *//'"),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_threads": psutil.cpu_count(logical=True),
        "cpu_flags_simd": [f for f in (_sh("grep -m1 flags /proc/cpuinfo") or "").split()
                           if f.startswith(("avx", "sse4", "fma", "f16c", "amx", "vnni"))],
        "cpu_governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "cpu_epp": _read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
        "cpu_max_mhz": _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"),
        "intel_no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "ram_total_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "on_ac_power": (batt.power_plugged if batt else None),
        "battery_pct": (batt.percent if batt else None),
        "gpu": _sh("lspci | grep -i -E 'vga|3d' | sed 's/.*: //'"),
        "load_avg_1m_at_start": psutil.getloadavg()[0],
        "packages": pkgs,
    }


if __name__ == "__main__":
    print(json.dumps(capture(), indent=2))
