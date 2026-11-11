#!/usr/bin/env python3
"""Build comparison tables and plots from one or more run directories.

  .venv/bin/python report.py results/<run>                    # one run vs its baseline
  .venv/bin/python report.py results/<runA> results/<runB>    # compare runs (first baseline = reference)
"""
import argparse
import json
from pathlib import Path

import pandas as pd
import yaml


def load(run: Path):
    rows = []
    for s in sorted(run.glob("*/summary.json")):
        d = json.load(open(s))
        sysd = d.pop("sys", {})
        wc = d.pop("wer_by_category", {})
        d.pop("config", None)
        d.update({f"wer_{k}": v for k, v in wc.items()})
        d.update(sysd)
        d["run"] = run.name
        rows.append(d)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--config", default="configs/candidates.yaml")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    tg = cfg.get("targets", {})

    df = pd.concat([load(Path(r)) for r in a.runs], ignore_index=True)
    if df.empty:
        print("no results found")
        return
    ref = df[df.name == "baseline"].iloc[0] if (df.name == "baseline").any() else df.iloc[0]
    df["wer_delta_pp"] = (df.wer - ref.wer) * 100
    df["speedup_vs_base"] = ref.rtf_overall / df.rtf_overall
    df["ram_delta_mb"] = df.rss_peak_mb - ref.rss_peak_mb

    def verdict(r):
        fails = []
        if tg.get("rtf") and r.rtf_overall > tg["rtf"]: fails.append("rtf")
        if tg.get("ttft_s") and r.ttft_p50_s > tg["ttft_s"]: fails.append("ttft")
        if tg.get("wer_delta_pp") is not None and r.wer_delta_pp > tg["wer_delta_pp"]: fails.append("wer")
        if tg.get("rss_mb") and r.rss_peak_mb > tg["rss_mb"]: fails.append("ram")
        if tg.get("max_hallucinated_words") is not None and r.hallucinated_words_on_silence > tg["max_hallucinated_words"]:
            fails.append("halluc")
        return "PASS" if not fails else "fail:" + ",".join(fails)
    df["verdict"] = df.apply(verdict, axis=1)

    out = Path(a.runs[-1])
    df.to_csv(out / "comparison.csv", index=False)

    cols = ["run", "name", "wer", "wer_delta_pp", "rtf_overall", "speedup_vs_base", "latency_p50_s",
            "latency_p95_s", "ttft_p50_s", "load_time_s", "rss_peak_mb", "model_file_mb",
            "proc_cpu_pct_mean", "cpu_freq_mhz_mean", "cpu_pkg_temp_c_max",
            "hallucinated_words_on_silence", "verdict"]
    cols = [c for c in cols if c in df]
    t = df[cols].copy()
    for c in t.select_dtypes("float").columns:
        t[c] = t[c].round(3 if c in ("wer", "rtf_overall", "latency_p50_s", "latency_p95_s", "ttft_p50_s") else 1)
    md = "# STT benchmark comparison\n\nTargets: " + json.dumps(tg) + "\n\n" + t.to_markdown(index=False) \
        if hasattr(t, "to_markdown") and _has_tabulate() else \
        "# STT benchmark comparison\n\n```\n" + t.to_string(index=False) + "\n```\n"
    (out / "report.md").write_text(md)
    print(t.to_string(index=False))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.scatter(df.rtf_overall, df.wer * 100)
        for _, r in df.iterrows():
            ax.annotate(r["name"], (r.rtf_overall, r.wer * 100), fontsize=7)
        ax.set_xlabel("real-time factor (lower = faster)")
        ax.set_ylabel("WER % (lower = better)")
        ax.axvline(tg.get("rtf", 0.5), ls="--", c="gray")
        ax.set_title("Speed vs accuracy")
        fig.tight_layout()
        fig.savefig(out / "speed_vs_accuracy.png", dpi=130)
    except Exception as e:
        print("plot skipped:", e)


def _has_tabulate():
    try:
        import tabulate  # noqa
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()
