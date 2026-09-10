#!/usr/bin/env python3
"""Extract rising-edge timestamps from a CSV waveform.

Reads a CSV whose first column is time and whose remaining columns are
signals:

    Time [s],SYN,VP,FREQ_UP_2,FREQ_DN_2
    3.817841462,0.707,2.113,-0.068,-0.059

Clips to a time window, finds every rising edge on the requested channels
using a Schmitt trigger (hysteresis around a fixed level, timestamp taken
at the level crossing with linear interpolation) and writes the edge times
to a CSV shaped like the input - one column per signal, nothing but
timestamps:

    FREQ_UP_2,FREQ_DN_2
    3.8195592368,3.81953270455

    ./rising_edges.py 2368_50k.csv --start 3.83 --end 3.85 -o edges.csv

The file is streamed in chunks, so multi-hundred-MB captures work in
constant memory.
"""

import argparse
import csv
import os
import sys

import numpy as np
import pandas as pd

CHUNK_ROWS = 1_000_000
OVERLAP = 4096          # samples of the previous chunk kept for back-search
DEFAULT_CHANNELS = ["FREQ_UP_2", "FREQ_DN_2"]
DEFAULT_THRESHOLD = 2.5     # volts - mid rail of these 0..5 V logic signals
DEFAULT_HYSTERESIS = 1.0    # volts - half-width of the Schmitt band


# --------------------------------------------------------------------------
def read_header(path):
    """Return the stripped column names of `path`."""
    with open(path, "r", errors="replace") as fh:
        line = fh.readline()
    if not line:
        sys.exit(f"{path}: file is empty")
    return [c.strip() for c in line.rstrip("\r\n").split(",")]


def iter_chunks(path, time_col, channels, start, end):
    """Yield (t, {name: v}) numpy arrays for rows inside [start, end]."""
    reader = pd.read_csv(path, usecols=[time_col] + channels, chunksize=CHUNK_ROWS,
                         engine="c", on_bad_lines="skip", low_memory=False)
    for chunk in reader:
        chunk = chunk.apply(pd.to_numeric, errors="coerce")
        t = chunk[time_col].to_numpy(dtype=np.float64)
        keep = np.isfinite(t)
        if start is not None:
            keep &= t >= start
        if end is not None:
            keep &= t <= end
        if not keep.any():
            # Past the window already?  The time column is monotonic, so once
            # every row is beyond `end` there is nothing left to read.
            if end is not None and np.isfinite(t).any() and np.nanmin(t) > end:
                return
            continue
        yield t[keep], {c: chunk[c].to_numpy(dtype=np.float64)[keep] for c in channels}


def scan_levels(path, time_col, channels, start, end):
    """First pass: per-channel (min, max) over the clipped window."""
    lo = {c: np.inf for c in channels}
    hi = {c: -np.inf for c in channels}
    rows = 0
    for _, vals in iter_chunks(path, time_col, channels, start, end):
        rows += len(next(iter(vals.values())))
        for c, v in vals.items():
            v = v[np.isfinite(v)]
            if v.size:
                lo[c] = min(lo[c], float(v.min()))
                hi[c] = max(hi[c], float(v.max()))
    return rows, lo, hi


# --------------------------------------------------------------------------
class EdgeFinder:
    """Schmitt-trigger rising-edge detector fed one chunk at a time."""

    def __init__(self, mid, hyst):
        self.mid = mid
        self.upper = mid + hyst
        self.lower = mid - hyst
        self.armed = True       # True once the signal has gone below `lower`
        self.times = []
        self.tail_t = np.empty(0, dtype=np.float64)
        self.tail_v = np.empty(0, dtype=np.float64)

    def feed(self, t, v):
        n_new = len(t)
        if n_new == 0:
            return
        n_tail = len(self.tail_t)
        t = np.concatenate((self.tail_t, t)) if n_tail else t
        v = np.concatenate((self.tail_v, v)) if n_tail else v

        # Schmitt trigger: keep only the samples that cross either rail, then
        # a rising edge is any below -> above transition in that sequence.
        above = v >= self.upper
        below = v <= self.lower
        idx = np.flatnonzero(above | below)
        if idx.size:
            state = above[idx]                       # True = above upper rail
            prev = np.concatenate(([not self.armed], state[:-1]))
            trig = idx[state & ~prev]                # first sample above, after being below
            for j in trig:
                if j < n_tail:
                    continue                         # already emitted last chunk
                self.times.append(self._cross_time(t, v, j))
            self.armed = not bool(state[-1])

        keep = min(OVERLAP, len(t))
        self.tail_t = t[-keep:].copy()
        self.tail_v = v[-keep:].copy()

    def _cross_time(self, t, v, j):
        """Interpolated time at which v crossed `mid` on its way up to t[j]."""
        k = j
        while k > 0 and v[k - 1] > self.mid:
            k -= 1
        if k == 0 or not np.isfinite(v[k - 1]):
            return float(t[j])
        v0, v1 = v[k - 1], v[k]
        if v1 == v0:
            return float(t[k])
        return float(t[k - 1] + (self.mid - v0) * (t[k] - t[k - 1]) / (v1 - v0))


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Clip a CSV waveform and export rising-edge timestamps.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("csv", help="input CSV file")
    ap.add_argument("-o", "--output", required=True, help="output CSV file name")
    ap.add_argument("--start", type=float, help="clip start time in seconds")
    ap.add_argument("--end", type=float, help="clip end time in seconds")
    ap.add_argument("--relative", action="store_true",
                    help="treat --start/--end as offsets from the first sample")
    ap.add_argument("-c", "--channels", default=",".join(DEFAULT_CHANNELS),
                    help="comma-separated channel names")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="trigger level in volts")
    ap.add_argument("--hysteresis", type=float, default=DEFAULT_HYSTERESIS,
                    help="half-width of the Schmitt band in volts")
    ap.add_argument("--auto-levels", action="store_true",
                    help="derive the level and band from each channel's range over "
                         "the clipped window instead of using the fixed defaults")
    ap.add_argument("--min-amplitude", type=float, default=1.0,
                    help="with --auto-levels, warn if a channel swings less than this "
                         "(volts); an idle channel would be thresholded onto its noise")
    ap.add_argument("--long", action="store_true",
                    help="write channel,index,time_s,period_s,freq_hz rows instead")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit(f"{args.csv}: no such file")

    names = read_header(args.csv)
    time_col = names[0]
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    missing = [c for c in channels if c not in names]
    if missing:
        sys.exit(f"channel(s) not in {args.csv}: {', '.join(missing)}\n"
                 f"available: {', '.join(names[1:])}")

    start, end = args.start, args.end
    if args.relative:
        first = pd.read_csv(args.csv, usecols=[time_col], nrows=1).iloc[0, 0]
        first = float(first)
        start = None if start is None else first + start
        end = None if end is None else first + end
        print(f"first sample at {first:.9g} s; "
              f"window {'-inf' if start is None else f'{start:.9g}'} .. "
              f"{'inf' if end is None else f'{end:.9g}'} s")
    if start is not None and end is not None and end <= start:
        sys.exit("--end must be greater than --start")

    # Pass 1 - levels, only when asked for.
    if args.auto_levels:
        print("scanning for signal levels ...", flush=True)
        rows, vmin, vmax = scan_levels(args.csv, time_col, channels, start, end)
        if rows == 0:
            sys.exit("no samples inside the requested time window")
        print(f"{rows} samples in window")
    else:
        vmin = vmax = None

    finders = {}
    for c in channels:
        if vmin is None:
            mid, span = args.threshold, None
        else:
            lo, hi = vmin[c], vmax[c]
            if not np.isfinite(lo) or hi <= lo:
                sys.exit(f"{c}: channel is flat over the window, no edges to find")
            span = hi - lo
            if span < args.min_amplitude:
                print(f"warning: {c} swings only {span:.4g} V over this window - "
                      f"it looks idle; edges below are probably noise "
                      f"(set --threshold explicitly to override)", file=sys.stderr)
            mid = 0.5 * (lo + hi)
        hyst = 0.2 * span if span is not None else args.hysteresis
        finders[c] = EdgeFinder(mid, hyst)
        print(f"{c}: threshold {mid:.4g} V, hysteresis +/-{hyst:.4g} V"
              + (f"  (range {vmin[c]:.4g} .. {vmax[c]:.4g} V)" if vmin else ""))

    # Pass 2 - edges.
    print("finding rising edges ...", flush=True)
    rows = 0
    for t, vals in iter_chunks(args.csv, time_col, channels, start, end):
        rows += len(t)
        for c, f in finders.items():
            f.feed(t, vals[c])
    if rows == 0:
        sys.exit("no samples inside the requested time window")
    print(f"{rows} samples in window")

    write_output(args.output, channels, finders, args.long)


def write_output(path, channels, finders, long_format):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        if not long_format:
            w.writerow(channels)
            longest = max((len(finders[c].times) for c in channels), default=0)
            for i in range(longest):
                w.writerow([f"{finders[c].times[i]:.12g}"
                            if i < len(finders[c].times) else "" for c in channels])
        else:
            w.writerow(["channel", "index", "time_s", "period_s", "freq_hz"])
            for c in channels:
                times = finders[c].times
                for i, ts in enumerate(times):
                    if i:
                        dt = ts - times[i - 1]
                        w.writerow([c, i, f"{ts:.12g}", f"{dt:.12g}",
                                    f"{1.0 / dt:.12g}" if dt else ""])
                    else:
                        w.writerow([c, i, f"{ts:.12g}", "", ""])

    for c in channels:
        times = finders[c].times
        if len(times) > 1:
            d = np.diff(times)
            print(f"{c}: {len(times)} rising edges, "
                  f"mean period {d.mean() * 1e6:.4f} us "
                  f"({1.0 / d.mean() / 1e3:.4f} kHz), "
                  f"min {d.min() * 1e6:.4f} us, max {d.max() * 1e6:.4f} us")
        else:
            print(f"{c}: {len(times)} rising edges")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
