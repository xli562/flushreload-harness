#!/usr/bin/env python3
"""Measure Flush+Reload key-recovery accuracy under varying VCPU load.

exp1: fixed threshold T0 (calibrated once at zero load) at every level.
exp2: threshold recalibrated at each load level.

Each (level, rep) point runs on the VM in one SSH session so the load
generator survives across calibration and newspy. Loader is killed on
script exit, after newspy finishes.
"""

import argparse
import csv
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

SSH = "/mnt/c/Windows/System32/OpenSSH/ssh.exe"
HOST = "flrl"
VM_HOME = "~/Flush-Reload"
LOADER = f"{VM_HOME}/loadgen"
NEWSPY = f"{VM_HOME}/newspy"
CALIB = f"{VM_HOME}/calibration"

DEFAULT_LEVELS = list(range(0, 100, 10))
DEFAULT_REPS = 3
CALIB_RUNS = 10
WARMUP_S = 2
LOAD_MEAS_S = 3
NEWSPY_TIMEOUT_S = 300
CALIB_TIMEOUT_S = 60
LOADER_CPU = 0
SPY_CPU = 1

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "results"

RE_LOAD = re.compile(r"^LOAD_MEASURED\s+([\d.]+)\s*$", re.M)
RE_THRESH = re.compile(r"^THRESHOLD\s+(\d+)\s*$", re.M)
RE_THRESH_USED = re.compile(r"^THRESH_USED\s+(\d+)\s*$", re.M)
RE_ACTUAL = re.compile(r"^Actual key:[ \t]+([0-9a-f ]+)$", re.M)
RE_RECOV = re.compile(r"^Recovered key:[ \t]+([0-9a-f ]+)$", re.M)


def ssh_bash(script: str, timeout_s: int) -> str:
    """Run a bash script on the VM via ssh.exe; return stdout.

    :param script: Bash text passed to `bash -s` on the remote host.
    :param timeout_s: Hard subprocess timeout.
    :return: Remote stdout as text.
    :raises RuntimeError: on ssh error, timeout, or non-zero remote rc.
    """
    try:
        proc = subprocess.run(
            [SSH, HOST, "bash -s"],
            input=script, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SSH timed out after {timeout_s}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Remote script failed (rc={proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc.stdout


def parse_load(stdout: str) -> float:
    """Extract measured VCPU load (%)."""
    m = RE_LOAD.search(stdout)
    if not m:
        raise ValueError(f"LOAD_MEASURED missing from:\n{stdout}")
    return float(m.group(1))


def parse_thresholds(stdout: str) -> list[int]:
    """Extract all calibration threshold values."""
    return [int(x) for x in RE_THRESH.findall(stdout)]


def parse_thresh_used(stdout: str) -> int:
    """Extract the exact threshold actually passed to newspy."""
    m = RE_THRESH_USED.search(stdout)
    if not m:
        raise ValueError(f"THRESH_USED missing from:\n{stdout}")
    return int(m.group(1))


def parse_newspy_keys(stdout: str) -> tuple[bytes, bytes]:
    """Extract (actual, recovered) 16-byte keys from newspy output."""
    ma, mr = RE_ACTUAL.search(stdout), RE_RECOV.search(stdout)
    if not ma or not mr:
        raise ValueError(f"newspy keys missing from:\n{stdout}")
    a = bytes(int(b, 16) for b in ma.group(1).split())
    r = bytes(int(b, 16) for b in mr.group(1).split())
    if len(a) != 16 or len(r) != 16:
        raise ValueError(f"Unexpected key length: {a!r} {r!r}")
    return a, r


def byte_accuracy(actual: bytes, recovered: bytes) -> tuple[float, bool]:
    """Fraction of matching bytes and exact-match flag."""
    matched = sum(x == y for x, y in zip(actual, recovered))
    return matched / len(actual), matched == len(actual)


def _calib_emit_block(n_runs: int) -> str:
    """Emit bash that runs calibration n_runs times and echoes each threshold."""
    return (
        f"for i in $(seq 1 {n_runs}); do\n"
        f"  V=$(timeout {CALIB_TIMEOUT_S} taskset -c {SPY_CPU} {CALIB} | "
        "awk -F': ' '/Suggested cache hit/ {print $NF}')\n"
        f'  if [ -z "$V" ]; then echo CALIB_PARSE_ERROR; exit 2; fi\n'
        f'  echo "THRESHOLD $V"\n'
        f"done\n"
    )


def build_run_script(level: int, mode: str, exp: int, T0: int) -> str:
    """Build the remote bash script for one (level, rep) point.

    :param level: Target loader duty cycle (%).
    :param mode: Loader mode: 'mem', 'busy', 'target', or 'evict'.
    :param exp: 1 (use T0) or 2 (recalibrate).
    :param T0: Fixed threshold for exp1; ignored for exp2.
    :return: Bash script text.
    """
    if exp == 2:
        thresh_block = (
            f"SUM=0; N=0\n"
            + f"for i in $(seq 1 {CALIB_RUNS}); do\n"
            + f"  V=$(timeout {CALIB_TIMEOUT_S} taskset -c {SPY_CPU} {CALIB} | "
            + "awk -F': ' '/Suggested cache hit/ {print $NF}')\n"
            + '  if [ -z "$V" ]; then echo CALIB_PARSE_ERROR; exit 2; fi\n'
            + '  echo "THRESHOLD $V"\n'
            + "  SUM=$(( SUM + V )); N=$(( N + 1 ))\n"
            + "done\n"
            + "THRESH=$(( (SUM + N / 2) / N ))\n"
        )
    else:
        thresh_block = f'echo "THRESHOLD {T0}"\nTHRESH={T0}\n'

    return f"""set -u
trap 'pkill -x loadgen 2>/dev/null || true' EXIT INT TERM
pkill -x loadgen 2>/dev/null || true
sleep 0.2

taskset -c {LOADER_CPU} {LOADER} {mode} {level} >/dev/null 2>&1 &
LOADER_PID=$!
sleep {WARMUP_S}
if ! kill -0 $LOADER_PID 2>/dev/null; then
  echo "LOADER_DIED" >&2
  exit 3
fi

A=$(awk '{{print $14+$15}}' /proc/$LOADER_PID/stat)
A_WALL=$(awk '{{print $1}}' /proc/uptime)
sleep {LOAD_MEAS_S}
B=$(awk '{{print $14+$15}}' /proc/$LOADER_PID/stat)
B_WALL=$(awk '{{print $1}}' /proc/uptime)
USER_HZ=$(getconf CLK_TCK)
DELTA=$(( B - A ))
LOAD=$(awk -v t=$DELTA -v h=$USER_HZ -v a=$A_WALL -v b=$B_WALL \\
        'BEGIN{{ load = 100.0 * t / (h * (b - a));
                 if (load > 100.0) load = 100.0;
                 printf "%.1f", load }}')
echo "LOAD_MEASURED $LOAD"

{thresh_block}
echo "THRESH_USED $THRESH"

timeout {NEWSPY_TIMEOUT_S} taskset -c {SPY_CPU} {NEWSPY} -c $THRESH
NEWSPY_RC=$?
if [ $NEWSPY_RC -ne 0 ]; then
  echo "NEWSPY_TIMEOUT_OR_ERROR rc=$NEWSPY_RC" >&2
  exit 4
fi
"""


def calibrate_baseline() -> tuple[int, list[int]]:
    """Run calibration CALIB_RUNS times with no load. Return (mean, raw)."""
    script = (
        "set -u\n"
        "pkill -x loadgen 2>/dev/null || true\n"
        "sleep 0.2\n"
        + _calib_emit_block(CALIB_RUNS)
    )
    out = ssh_bash(script, timeout_s=CALIB_TIMEOUT_S * CALIB_RUNS + 60)
    raw = parse_thresholds(out)
    if len(raw) != CALIB_RUNS:
        raise RuntimeError(f"Expected {CALIB_RUNS} thresholds, got {len(raw)}: {raw}")
    return int(mean(raw) + 0.5), raw


def run_point(level: int, rep: int, mode: str, exp: int, T0: int) -> dict:
    """Execute one (level, rep) data point and return its record."""
    script = build_run_script(level, mode, exp, T0)
    out = ssh_bash(
        script,
        timeout_s=CALIB_TIMEOUT_S * CALIB_RUNS + NEWSPY_TIMEOUT_S + 60,
    )
    load = parse_load(out)
    thresholds = parse_thresholds(out)
    if not thresholds:
        raise RuntimeError(f"No THRESHOLD lines in:\n{out}")
    if exp == 2 and len(thresholds) != CALIB_RUNS:
        raise RuntimeError(f"Expected {CALIB_RUNS} thresholds, got {len(thresholds)}")
    actual, recovered = parse_newspy_keys(out)
    thresh_used = parse_thresh_used(out)
    acc, exact = byte_accuracy(actual, recovered)

    rec = {
        "level": level, "mode": mode, "rep": rep, "load_pct": load,
        "accuracy": acc, "exact_match": exact,
        "threshold_used": thresh_used,
        "actual_key": actual.hex(), "recovered_key": recovered.hex(),
    }
    if exp == 2:
        rec["threshold_mean"] = mean(thresholds)
        rec["threshold_std"] = pstdev(thresholds) if len(thresholds) > 1 else 0.0
        rec["thresholds_raw"] = thresholds
    return rec


def run_experiment(exp: int, levels: list[int], reps: int, mode: str) -> list[dict]:
    """Run one experiment across levels × reps."""
    T0 = 0
    if exp == 1:
        print("Calibrating T0 at zero load ...", flush=True)
        T0, raw = calibrate_baseline()
        print(f"  T0 = {T0}  raw = {raw}")
    records: list[dict] = []
    for level in levels:
        for rep in range(reps):
            print(f"[exp{exp}] level={level:>2}% rep={rep} ... ", end="", flush=True)
            rec = run_point(level, rep, mode, exp, T0)
            print(f"load={rec['load_pct']:5.1f}%  acc={rec['accuracy']:.3f}")
            records.append(rec)
    return records


def write_csv(path: Path, records: list[dict], exp: int) -> None:
    """Write records to CSV; columns depend on the experiment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        ["level", "mode", "load_pct", "accuracy", "exact_match",
         "threshold_used", "rep", "actual_key", "recovered_key"]
        if exp == 1 else
        ["level", "mode", "load_pct", "accuracy", "exact_match",
         "threshold_used", "threshold_mean", "threshold_std",
         "thresholds_raw", "rep", "actual_key", "recovered_key"]
    )
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            row = {k: r.get(k) for k in fields}
            if isinstance(row.get("thresholds_raw"), list):
                row["thresholds_raw"] = ";".join(map(str, row["thresholds_raw"]))
            w.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=["exp1", "exp2", "both"])
    ap.add_argument("--levels", type=int, nargs="+", default=DEFAULT_LEVELS)
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--mode", choices=["mem", "busy", "target", "evict"], default="mem")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.experiment in ("exp1", "both"):
        recs = run_experiment(1, args.levels, args.reps, args.mode)
        stamped_path = OUT_DIR / f"{stamp}_exp1_{args.mode}.csv"
        write_csv(stamped_path, recs, 1)
        write_csv(OUT_DIR / f"exp1_{args.mode}.csv", recs, 1)
        print(f"Wrote {stamped_path} and {OUT_DIR / f'exp1_{args.mode}.csv'}")

    if args.experiment in ("exp2", "both"):
        recs = run_experiment(2, args.levels, args.reps, args.mode)
        stamped_path = OUT_DIR / f"{stamp}_exp2_{args.mode}.csv"
        write_csv(stamped_path, recs, 2)
        write_csv(OUT_DIR / f"exp2_{args.mode}.csv", recs, 2)
        print(f"Wrote {stamped_path} and {OUT_DIR / f'exp2_{args.mode}.csv'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
