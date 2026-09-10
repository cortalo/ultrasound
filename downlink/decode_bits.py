#!/usr/bin/env python3
"""Decode FSK rising-edge timestamps into a bit stream.

Takes the wide edge CSV written by rising_edges.py:

    FREQ_UP_2,FREQ_DN_2
    3.81955923783,3.81953270496

A run of consecutive rising edges on FREQ_DN_2 is symbol 0, a run on
FREQ_UP_2 is symbol 1.  The two edge lists are merged in time and split
into runs; the boundary between two runs is the midpoint between the last
edge of one and the first edge of the next.  The first window starts at
the first edge and the last window ends at the last edge.

How a window is turned into a symbol count depends on --method:

    midspan     boundary halfway between the two edges either side of it,
                with a rounding threshold per channel to absorb each one's
                bias.  Works at both symbol rates            [default]
    midpoint    the same boundary, rounded at 0.5  (the naive rule)
    halfperiod  boundary extrapolated half an edge-period past the last
                edge and half an edge-period before the next one, which
                centres it in the dead zone between the two tones
    grid        halfperiod boundaries, then a symbol clock (period and
                phase) is least-squares fitted to them and each boundary
                snapped to that grid; a window spans the difference of
                the two grid indices                          [default]
    vote        same recovered clock, but the runs are ignored: each
                20 us slot is scored by which channel has more edges in it
    dnspan      no boundaries at all: the zero channel is measured by the
                span of its own edges and the one channel takes everything
                between two zero-channel bursts, each with its own rounding
                threshold (--dn-threshold / --up-threshold)

The midpoint rule is biased.  There is roughly 11 us of dead time between
the last edge of one tone and the first edge of the next, and splitting it
evenly is only right when it is symmetric - where it is not, a boundary
lands up to two thirds of a symbol off and a window miscounts.

    ./decode_bits.py edges_2368_50k.csv -o bits.txt

A lone edge bracketed by two edges of the other channel less than --glitch
(4 us) apart is a stray burst inside that channel's tone, not a symbol of
its own, and is removed before the runs are built.  Any window left under
--min-symbol (10 us) would round to zero symbols and is reported as an
error.
"""

import argparse
import math
import sys

import numpy as np
import pandas as pd


DEFAULT_ONE = "FREQ_UP_2"
DEFAULT_ZERO = "FREQ_DN_2"


# --------------------------------------------------------------------------
def load_events(path, one_col, zero_col):
    """Return (times, symbols) for every edge, sorted by time."""
    d = pd.read_csv(path)
    names = [str(c).strip() for c in d.columns]
    d.columns = names
    for c in (one_col, zero_col):
        if c not in names:
            sys.exit(f"column {c!r} not in {path}\navailable: {', '.join(names)}")

    times, syms = [], []
    for col, sym in ((zero_col, 0), (one_col, 1)):
        v = pd.to_numeric(d[col], errors="coerce").to_numpy(dtype=np.float64)
        v = v[np.isfinite(v)]
        times.append(v)
        syms.append(np.full(v.shape, sym, dtype=np.int8))
    t = np.concatenate(times)
    s = np.concatenate(syms)
    order = np.argsort(t, kind="stable")
    return t[order], s[order]


def find_runs(t, s):
    """Split the edge stream into runs of one symbol.

    Returns a list of [symbol, first_edge_time, last_edge_time, n_edges].
    """
    if t.size == 0:
        return []
    change = np.flatnonzero(s[1:] != s[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [t.size]))       # exclusive
    return [[int(s[a]), float(t[a]), float(t[b - 1]), int(b - a)]
            for a, b in zip(starts, ends)]


def window_edges(runs):
    """Window boundaries: midway between the runs on either side."""
    mids = [0.5 * (runs[i][2] + runs[i + 1][1]) for i in range(len(runs) - 1)]
    starts = [runs[0][1]] + mids            # first window starts at the first edge
    ends = mids + [runs[-1][2]]             # last window ends at the last edge
    return starts, ends


def remove_glitch_edges(t, s, span):
    """Drop lone edges that sit inside the other channel's burst.

    A glitch shows up as a single edge of one channel bracketed by two
    edges of the other.  When those two flanking edges are less than `span`
    apart they belong to one continuous burst - at a real symbol boundary
    the two channels are separated by the 10-18 us dead zone - so the edge
    between them is spurious and is removed.

    Removing one edge can expose another (a glitch pair leaves its partner
    bracketed by the burst it interrupted), so the scan repeats until the
    edge list stops changing.

    Returns (t, s, removed) with `removed` listing (time, symbol) pairs.
    """
    t = list(t)
    s = list(s)
    removed = []
    while True:
        hit = next((i for i in range(1, len(t) - 1)
                    if s[i - 1] == s[i + 1] != s[i]
                    and t[i + 1] - t[i - 1] < span), None)
        if hit is None:
            break
        removed.append((t[hit], s[hit]))
        del t[hit]
        del s[hit]
    return (np.array(t, dtype=np.float64),
            np.array(s, dtype=np.int8), removed)


def edge_periods(runs):
    """Median edge period within each run, for runs that have one."""
    per = np.array([(r[2] - r[1]) / (r[3] - 1) if r[3] > 1 else np.nan
                    for r in runs])
    if np.isnan(per).all():
        return None
    return np.where(np.isfinite(per), per, np.nanmedian(per))


def halfperiod_edges(runs):
    """Boundaries centred in the dead zone between two tones.

    A tone that starts on a symbol boundary produces its first edge up to
    one edge-period later, and its last edge up to one edge-period before
    the next boundary.  Extrapolating each side by half its own period puts
    the boundary in the middle of the gap instead of midway between two
    edges that are not symmetric about it.
    """
    per = edge_periods(runs)
    if per is None:
        return window_edges(runs)
    mids = [0.5 * ((runs[i][2] + per[i] / 2) + (runs[i + 1][1] - per[i + 1] / 2))
            for i in range(len(runs) - 1)]
    return ([runs[0][1] - per[0] / 2] + mids,
            mids + [runs[-1][2] + per[-1] / 2])


def counts_from_edgespan(runs, period, edge_width):
    """Count symbols from the run's own edges, ignoring the dead time.

    Each edge is treated as `edge_width` wide, so a lone edge is a window of
    that width rather than nothing, and the division rounds up: a run of k
    symbols spans k * period minus the dead time at each end, so as long as
    that dead time falls in [edge_width, period + edge_width) the ceiling
    lands on k.
    """
    return [max(1, math.ceil((r[2] - r[1] + edge_width) / period))
            for r in runs]


def counts_from_midspan(runs, period, dn_thr, up_thr):
    """Midpoint boundaries, with a rounding threshold per channel.

    The boundary between two runs is the midpoint of the last edge of one
    and the first edge of the next, so the ~11 us dead zone between the two
    tones is split evenly.  It is not shared evenly in truth - the faster
    tone fills more of its symbol with edges than the slower one - which
    leaves each channel's fractional symbol count biased its own way.  One
    rounding threshold per channel absorbs that bias.

    Unlike dnspan this needs no channel to be well populated with edges, so
    it survives a shorter symbol period: at 15 us the dead zone takes 74%
    of a symbol and dnspan has nothing left to measure, while this still
    decodes.
    """
    starts, ends = window_edges(runs)
    counts = []
    for i, run in enumerate(runs):
        thr = dn_thr if run[0] == 0 else up_thr
        counts.append(max(1, math.floor((ends[i] - starts[i]) / period
                                        + (1.0 - thr))))
    return counts


def counts_from_dnspan(runs, period, dn_thr, up_thr):
    """Measure the zero channel by its own edges, give the rest to the one channel.

    The two tones fill their symbols differently - the faster one (symbol 0
    here) very nearly fills its window with edges, the slower one leaves a
    long dead zone at each end - so a boundary between them is never in the
    same place relative to either.  Instead of trying to find it, the zero
    channel's window is just the span of its own edges, and the one channel
    gets everything between one burst and the next.

    That makes each channel's fractional symbol count sit in its own band,
    so each gets its own rounding threshold: a window rounds up when its
    fraction reaches that threshold and down otherwise.  On the reference
    capture the fractions leave an empty band at 0.10-0.41 for the zero
    channel and 0.58-0.85 for the one channel, which is where the 0.3 and
    0.7 defaults come from.
    """
    counts = []
    for i, run in enumerate(runs):
        if run[0] == 0:
            dur, thr = run[2] - run[1], dn_thr
        else:
            first = runs[i - 1][2] if i > 0 else run[1]
            last = runs[i + 1][1] if i < len(runs) - 1 else run[2]
            dur, thr = last - first, up_thr
        counts.append(max(1, math.floor(dur / period + (1.0 - thr))))
    return counts


def fit_grid(bounds, period, iters=20):
    """Least-squares fit `bounds` to t0 + k * period, refining both.

    The symbol clock is what the boundaries are noisy measurements of, so
    fitting it and snapping to it removes the per-boundary error instead of
    letting each window round independently.
    """
    b = np.asarray(bounds, dtype=np.float64)
    t0, T = b[0], period
    for _ in range(iters):
        k = np.round((b - t0) / T)
        prev = (t0, T)
        (t0, T) = np.linalg.lstsq(np.vstack([np.ones_like(k), k]).T, b,
                                  rcond=None)[0]
        if np.allclose(prev, (t0, T), rtol=0, atol=1e-15):
            break
    k = np.round((b - t0) / T).astype(int)
    resid = (b - t0) - k * T
    return t0, T, k, resid


def counts_from_grid(runs, starts, ends, period):
    """Symbol counts as differences of snapped grid indices."""
    t0, T, k, resid = fit_grid(ends[:-1], period)
    ks = np.concatenate(([int(round((starts[0] - t0) / T))], k,
                         [int(round((ends[-1] - t0) / T))]))
    return list(np.diff(ks)), (t0, T, resid)


def run_at(runs, mids, when):
    """Symbol of the run covering `when`, else of the nearest run."""
    i = int(np.searchsorted(mids, when))
    i = min(max(i, 0), len(runs) - 1)
    for j in (i, i - 1, i + 1):         # a slot centre in a dead zone lands
        if 0 <= j < len(runs):          # between two runs; take the closest
            if runs[j][1] <= when <= runs[j][2]:
                return runs[j][0]
    best = min(range(max(0, i - 1), min(len(runs), i + 2)),
               key=lambda j: min(abs(when - runs[j][1]), abs(when - runs[j][2])))
    return runs[best][0]


def counts_from_vote(t, s, runs, starts, ends, period):
    """Score each symbol slot by which channel has more edges in it.

    A slot can come out even two ways.  If it straddles a tone change both
    channels contribute edges, and the run covering the slot centre says
    which tone held the slot.  If it is empty the slot sat in the dead zone
    between two tones and there is no evidence in it at all, so it keeps the
    previous symbol - the nearest run is as likely to be the one the signal
    is leaving as the one it is entering.
    """
    t0, T, _, resid = fit_grid(ends[:-1], period)
    k0 = math.floor((t[0] - t0) / T + 0.5)
    k1 = math.floor((t[-1] - t0) / T + 0.5)
    grid = t0 + np.arange(k0, k1 + 1) * T
    n_one = np.histogram(t[s == 1], bins=grid)[0]
    n_zero = np.histogram(t[s == 0], bins=grid)[0]
    mids = np.array([0.5 * (r[1] + r[2]) for r in runs])
    bits, ties, empty, last = [], 0, 0, "0"
    for i, (u, d) in enumerate(zip(n_one, n_zero)):
        if u != d:
            last = "1" if u > d else "0"
        elif u:                         # contested: the runs know which tone
            ties += 1
            last = str(run_at(runs, mids, 0.5 * (grid[i] + grid[i + 1])))
        else:                           # empty slot: no evidence, hold
            empty += 1
        bits.append(last)
    return "".join(bits), (t0, T, resid), (ties, empty)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Decode rising-edge timestamps into a 0/1 bit stream.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("edges", help="edge CSV from rising_edges.py")
    ap.add_argument("-o", "--output", required=True, help="output text file name")
    ap.add_argument("--symbol", type=float, default=20.0,
                    help="symbol period in microseconds")
    ap.add_argument("--glitch", type=float, default=4.0,
                    help="a lone edge bracketed by two edges of the other channel "
                         "less than this far apart (microseconds) is spurious "
                         "and is removed")
    ap.add_argument("--min-symbol", type=float,
                    help="error out on any surviving window shorter than this "
                         "(microseconds); default is half a symbol")
    ap.add_argument("--one", default=DEFAULT_ONE, help="column carrying symbol 1")
    ap.add_argument("--zero", default=DEFAULT_ZERO, help="column carrying symbol 0")
    ap.add_argument("--method", default="midspan",
                    choices=("midspan", "midpoint", "halfperiod", "grid",
                             "vote", "edgespan", "dnspan"),
                    help="how window boundaries and symbol counts are decided")
    ap.add_argument("--edge-width", type=float, default=1.0,
                    help="width in microseconds attributed to a single edge, "
                         "used by --method edgespan")
    ap.add_argument("--dn-threshold", type=float,
                    help="fraction at which a --zero channel window rounds up "
                         "(default 0.85 for midspan, 0.22 for dnspan)")
    ap.add_argument("--up-threshold", type=float,
                    help="fraction at which a --one channel window rounds up "
                         "(default 0.15 for midspan, 0.75 for dnspan)")
    ap.add_argument("--wrap", type=int, default=0,
                    help="wrap the output every N bits (0 = one long line)")
    args = ap.parse_args()

    dn_default, up_default = ((0.85, 0.15) if args.method == "midspan"
                              else (0.22, 0.75))
    if args.dn_threshold is None:
        args.dn_threshold = dn_default
    if args.up_threshold is None:
        args.up_threshold = up_default
    if args.min_symbol is None:
        args.min_symbol = args.symbol / 2.0

    t, s = load_events(args.edges, args.one, args.zero)
    if t.size == 0:
        sys.exit(f"{args.edges}: no edges found")
    t0 = float(t[0])
    n_edges = t.size
    t, s, removed = remove_glitch_edges(t, s, args.glitch * 1e-6)
    runs = find_runs(t, s)
    period = args.symbol * 1e-6
    if args.method in ("midpoint", "midspan"):
        starts, ends = window_edges(runs)
    elif args.method == "edgespan":
        starts = [r[1] for r in runs]
        ends = [r[2] for r in runs]
    elif args.method == "dnspan":
        starts = [r[1] if r[0] == 0 else (runs[i - 1][2] if i else r[1])
                  for i, r in enumerate(runs)]
        ends = [r[2] if r[0] == 0
                else (runs[i + 1][1] if i < len(runs) - 1 else r[2])
                for i, r in enumerate(runs)]
    else:
        starts, ends = halfperiod_edges(runs)

    print(f"{n_edges} edges, {len(removed)} glitch edge(s) removed, "
          f"{len(runs)} windows, span {(ends[-1] - starts[0]) * 1e3:.6f} ms, "
          f"method {args.method}")

    for tg, sym in removed:
        print(f"  removed glitch edge on {args.one if sym else args.zero} "
              f"at t = {tg:.9f} s (+{(tg - t0) * 1e6:.2f} us)")

    if args.method == "vote":
        bits, (t0, T, resid), (ties, empty) = counts_from_vote(
            t, s, runs, starts, ends, period)
        report_clock(t0, T, resid, period)
        if ties or empty:
            print(f"{ties} contested slot(s) decided by run, "
                  f"{empty} empty slot(s) held the previous symbol")
        write_bits(args.output, bits, args.wrap)
        summarise(bits, None)
        print(f"wrote {args.output}")
        return

    if args.method == "grid":
        counts, (t0, T, resid) = counts_from_grid(runs, starts, ends, period)
        report_clock(t0, T, resid, period)
    elif args.method == "edgespan":
        counts = counts_from_edgespan(runs, period, args.edge_width * 1e-6)
    elif args.method == "dnspan":
        counts = counts_from_dnspan(runs, period, args.dn_threshold,
                                    args.up_threshold)
    elif args.method == "midspan":
        counts = counts_from_midspan(runs, period, args.dn_threshold,
                                     args.up_threshold)
    else:
        counts = [math.floor((e - st) / period + 0.5)   # 4 down / 5 up
                  for st, e in zip(starts, ends)]

    # The clip starts and ends wherever the user cut it, so the first and
    # last windows are partial by construction - drop them if they came out
    # empty rather than calling them errors.
    partial = []
    for i in (0, len(counts) - 1):
        if counts[i] < 1:
            partial.append((i, ends[i] - starts[i]))
            counts[i] = 0
    for i, dur in partial:
        print(f"dropped partial window {i} at the clip edge ({dur * 1e6:.3f} us)")

    # Check every window before emitting anything, so one report lists them all.
    bad = []
    for i, (start, end) in enumerate(zip(starts, ends)):
        dur = end - start
        if i in (0, len(counts) - 1) and counts[i] == 0:
            continue
        if counts[i] < 1 or (args.method not in ("edgespan", "dnspan", "midspan")
                             and dur < args.min_symbol * 1e-6):
            bad.append((i, dur, counts[i]))

    if bad:
        print(f"\nerror: {len(bad)} window(s) too short to be a symbol and too "
              f"long to be a burst:", file=sys.stderr)
        for i, dur, n in bad:
            sym = runs[i][0]
            print(f"  window {i}: symbol {sym} on "
                  f"{args.one if sym else args.zero}, {runs[i][3]} edge(s), "
                  f"{dur * 1e6:.3f} us -> {n} symbols, "
                  f"t = {starts[i]:.9f} s (+{(starts[i] - t0) * 1e6:.2f} us)",
                  file=sys.stderr)
        sys.exit(2)

    bits = "".join(str(runs[i][0]) * n for i, n in enumerate(counts))
    write_bits(args.output, bits, args.wrap)
    summarise(bits, np.array(counts))
    print(f"wrote {args.output}")


def report_clock(t0, T, resid, nominal):
    rms = float(np.sqrt(np.mean(resid ** 2)))
    print(f"symbol clock: period {T * 1e6:.6f} us "
          f"({(T / nominal - 1) * 1e6:+.0f} ppm vs nominal), "
          f"phase {t0:.9f} s, boundary residual {rms * 1e9:.0f} ns rms "
          f"({rms / T * 100:.1f}% of a symbol)")
    if abs(T / nominal - 1) > 0.05:
        print("warning: fitted period is more than 5% off nominal - the clock "
              "fit probably locked onto the wrong grid", file=sys.stderr)


def write_bits(path, bits, wrap):
    with open(path, "w") as fh:
        if wrap > 0:
            for i in range(0, len(bits), wrap):
                fh.write(bits[i:i + wrap] + "\n")
        else:
            fh.write(bits + "\n")


def summarise(bits, counts):
    line = (f"{len(bits)} bits ({bits.count('0')} zeros, "
            f"{bits.count('1')} ones)")
    if counts is not None:
        line += f", longest run {counts.max()} symbols"
    print(line)
    print(f"first 64 bits: {bits[:64]}")


if __name__ == "__main__":
    main()
