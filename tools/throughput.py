#!/usr/bin/env python3
"""Throughput monitor: how busy are the CPU, RAM and GPU, and how long do the batch stages take.

    python3 -I tools/throughput.py sample [--every 15] [--out FILE]   # run in the background
    python3 -I tools/throughput.py report [--samples FILE] [--logs LOG ...] [--since HH:MM]

`sample` appends one CSV row per interval: CPU busy % (all 24 threads), load, RAM used, the biggest process
(RSS), ComfyUI's VRAM in use and whether its queue is running (the GPU duty-cycle proxy: nvidia-smi is not
installed here, so "GPU busy" is the fraction of samples with a ComfyUI job running).
`report` summarises the samples and the per-stage seconds that tools/batch.py logs ("<id>: paint ok in 170.4s")
so two runs (e.g. the 30 GB box vs the 64 GB box) can be compared.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SCRATCH = Path.home() / ".cache" / "monster-forge-scratch"
DEFAULT_OUT = SCRATCH / "throughput.csv"
COMFY = "http://127.0.0.1:8188"
FIELDS = ["t", "cpu_pct", "load1", "ram_used_gb", "ram_avail_gb", "top_proc", "top_rss_gb", "vram_used_gb",
          "vram_total_gb", "comfy_running", "comfy_pending", "blender_procs", "forge_procs"]


def _cpu_times() -> tuple[int, int]:
    f = open("/proc/stat").readline().split()[1:]
    v = list(map(int, f))
    idle = v[3] + v[4]
    return sum(v), idle


def _get(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _meminfo() -> tuple[float, float]:
    m = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        m[k] = int(v.split()[0])
    total, avail = m["MemTotal"], m["MemAvailable"]
    return (total - avail) / 2**20, avail / 2**20


def _top_proc() -> tuple[str, float]:
    out = subprocess.run(["ps", "-eo", "rss=,comm=", "--sort=-rss"], capture_output=True, text=True).stdout.splitlines()
    if not out:
        return "", 0.0
    rss, comm = out[0].strip().split(None, 1)
    return comm, int(rss) / 2**20


def _count(pattern: str) -> int:
    r = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


def sample(every: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists()
    prev = _cpu_times()
    with open(out, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FIELDS)
        while True:
            time.sleep(every)
            cur = _cpu_times()
            dt, di = cur[0] - prev[0], cur[1] - prev[1]
            prev = cur
            cpu = 100.0 * (1 - di / dt) if dt else 0.0
            used, avail = _meminfo()
            comm, rss = _top_proc()
            st = _get(COMFY + "/system_stats")
            vu = vt = 0.0
            if st and st.get("devices"):
                d = st["devices"][0]
                vt = d["vram_total"] / 2**30
                vu = (d["vram_total"] - d["vram_free"]) / 2**30
            q = _get(COMFY + "/queue") or {}
            w.writerow([time.strftime("%H:%M:%S"), f"{cpu:.0f}", f"{os.getloadavg()[0]:.1f}", f"{used:.1f}",
                        f"{avail:.1f}", comm, f"{rss:.1f}", f"{vu:.1f}", f"{vt:.1f}",
                        len(q.get("queue_running", [])), len(q.get("queue_pending", [])),
                        _count("[b]lender"), _count("tools/[f]orge.py")])
            fh.flush()


STAGE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] batch: (\S+): (\w+) ok in ([\d.]+)s")


def stage_times(logs: list[Path], since: str | None) -> dict[str, list[float]]:
    by: dict[str, list[float]] = {}
    for lg in logs:
        if not lg.exists():
            continue
        for line in lg.read_text(errors="replace").splitlines():
            m = STAGE_RE.match(line)
            if not m:
                continue
            t, _mid, stage, secs = m.groups()
            if since and t < since + ":00":
                continue
            by.setdefault(stage, []).append(float(secs))
    return by


def report(samples: Path, logs: list[Path], since: str | None) -> None:
    by = stage_times(logs, since)
    print("stage seconds per monster (batch log):")
    for st in ("ref", "mesh", "qa", "paint", "lod", "rig", "anim"):
        v = by.get(st)
        if v:
            print(f"  {st:6s} n={len(v):3d} median {statistics.median(v):7.1f}s  mean {statistics.mean(v):7.1f}s  total {sum(v)/60:6.1f} min")
    if by:
        per = sum(statistics.median(by[s]) for s in ("mesh", "paint", "rig", "lod", "anim", "qa") if s in by)
        print(f"  one monster, all stages (sum of medians, no ref): {per/60:.1f} min")
    if not samples.exists():
        return
    rows = list(csv.DictReader(open(samples)))
    if since:
        rows = [r for r in rows if r["t"] >= since + ":00"]
    if not rows:
        print("no samples")
        return
    f = lambda k: [float(r[k]) for r in rows]
    cpu, ram = f("cpu_pct"), f("ram_used_gb")
    busy = [int(r["comfy_running"]) > 0 for r in rows]
    vram = f("vram_used_gb")
    print(f"\nsamples: {len(rows)} ({rows[0]['t']} to {rows[-1]['t']})")
    print(f"  CPU busy: mean {statistics.mean(cpu):.0f}%  p90 {sorted(cpu)[int(.9*len(cpu))-1]:.0f}%  max {max(cpu):.0f}%  (24 threads)")
    print(f"  RAM used: mean {statistics.mean(ram):.1f} GB  max {max(ram):.1f} GB   lowest available {min(f('ram_avail_gb')):.1f} GB")
    print(f"  GPU duty (ComfyUI job running): {100*sum(busy)/len(busy):.0f}% of samples; VRAM mean {statistics.mean(vram):.1f} / max {max(vram):.1f} GB of {rows[-1]['vram_total_gb']}")
    both = sum(1 for r, b in zip(rows, busy) if b and float(r["cpu_pct"]) > 40)
    print(f"  GPU job and CPU > 40% at the same time: {100*both/len(rows):.0f}% of samples (overlap)")
    print(f"  Blender processes: max {max(int(r['blender_procs']) for r in rows)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--every", type=float, default=15)
    s.add_argument("--out", type=Path, default=DEFAULT_OUT)
    r = sub.add_parser("report")
    r.add_argument("--samples", type=Path, default=DEFAULT_OUT)
    r.add_argument("--logs", type=Path, nargs="*", default=[SCRATCH / "resume.log"])
    r.add_argument("--since", help="HH:MM, only newer samples and stages")
    a = ap.parse_args()
    if a.cmd == "sample":
        try:
            sample(a.every, a.out)
        except KeyboardInterrupt:
            pass
    else:
        report(a.samples, a.logs, a.since)
    return 0


if __name__ == "__main__":
    sys.exit(main())
