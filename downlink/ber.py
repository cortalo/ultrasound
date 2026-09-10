#!/usr/bin/env python3
"""Decode one FSK capture and score it against a golden bit stream.

Runs the whole chain in one pass:

    CSV waveform -> rising edges -> glitch removal -> symbols -> BER

    ./ber.py 5000_50k.csv --start 4.188950 --end 4.277750 \
             --golden golden_5000.txt

The decode is fixed to the settings that were validated against the
reference captures, so the only things to choose are the clip window and
the golden file:

    2.5 V trigger, +/-1 V hysteresis        rising-edge detection
    4 us glitch span                        lone edge inside the other
                                            channel's burst is dropped
    20 us symbol period                     --symbol overrides it; run_ber.sh
                                            drives the other rates that way
    0.85 / 0.15 rounding thresholds         window boundaries midway between
                                            the edges either side, one
                                            rounding threshold per channel

Choosing the clip: start a few ms after SYN leaves its idle frequency
(2 x the low tone) so the PLL has settled, and end before FREQ_UP_2 stops
pulsing while FREQ_DN_2 doubles its rate - past that the FSK has failed.
--scan reports where the errors sit so a bad clip is easy to spot.
"""

import argparse
import math
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rising_edges import EdgeFinder, iter_chunks, read_header
from decode_bits import counts_from_midspan, find_runs, remove_glitch_edges

THRESHOLD = 2.5         # V
HYSTERESIS = 1.0        # V
GLITCH_SPAN = 4e-6      # s
SYMBOL = 20e-6          # s
DN_THRESHOLD = 0.85     # plateau is 0.80-0.90 at both symbol rates
UP_THRESHOLD = 0.15     # plateau is 0.10-0.20
ONE_CH, ZERO_CH = "FREQ_UP_2", "FREQ_DN_2"
SYNC = "00011010110011111111110000011101"
STAGE1_CELL = "0000011111"     # one 100 us level pair, repeated through stage 1


def stage1_length(golden):
    """Length of the leading stage-1 run of repeated 100 us level pairs.

    Stage 1 is the transmitter warming up and the PLL acquiring; its long
    runs decode far more easily than payload data, and the errors in it are
    acquisition transients rather than link errors, so it is skipped by
    default rather than diluting the BER.
    """
    n = 0
    while golden.startswith(STAGE1_CELL, n):
        n += len(STAGE1_CELL)
    return n


def count_symbols(runs):
    """Symbols per window."""
    return counts_from_midspan(runs, SYMBOL, DN_THRESHOLD, UP_THRESHOLD)


def extract_edges(path, start, end):
    """Rising-edge times for the two FSK channels, merged and time-sorted."""
    names = read_header(path)
    missing = [c for c in (ONE_CH, ZERO_CH) if c not in names]
    if missing:
        sys.exit(f"{path}: missing channel(s) {', '.join(missing)}\n"
                 f"available: {', '.join(names[1:])}")
    finders = {c: EdgeFinder(THRESHOLD, HYSTERESIS) for c in (ONE_CH, ZERO_CH)}
    rows = 0
    for t, vals in iter_chunks(path, names[0], [ONE_CH, ZERO_CH], start, end):
        rows += len(t)
        for c, f in finders.items():
            f.feed(t, vals[c])
    if rows == 0:
        sys.exit("no samples inside the requested time window")
    times = np.concatenate([np.asarray(finders[ZERO_CH].times),
                            np.asarray(finders[ONE_CH].times)])
    syms = np.concatenate([np.zeros(len(finders[ZERO_CH].times), np.int8),
                           np.ones(len(finders[ONE_CH].times), np.int8)])
    order = np.argsort(times, kind="stable")
    return rows, times[order], syms[order]


def read_bits(path):
    """Every 0/1 character in `path`, whitespace and comments ignored."""
    text = "".join(l for l in open(path) if not l.lstrip().startswith("#"))
    bits = re.sub(r"[^01]", "", text)
    if not bits:
        sys.exit(f"{path}: no 0/1 characters found")
    return bits


def count_errors(bits, ref, lookahead=24):
    """Errors between `bits` and `ref`, tolerating insertions and deletions.

    A dropout that swallows a symbol shifts everything after it, and scoring
    at a fixed offset would then count the rest of the stream as noise - one
    slip can turn a 1.5% error rate into an apparent 50%.  Resynchronising on
    a run of `lookahead` matching bits separates the handful of real slips
    from the substitutions they would otherwise mask.

    Returns (substitutions, insertions, deletions, reference bits covered).
    """
    i = j = sub = ins = dele = 0
    while i < len(bits) - lookahead and j < len(ref) - lookahead:
        if bits[i] == ref[j]:
            i += 1
            j += 1
        elif bits[i + 1:i + 1 + lookahead] == ref[j + 1:j + 1 + lookahead]:
            sub += 1
            i += 1
            j += 1
        elif bits[i + 1:i + 1 + lookahead] == ref[j:j + lookahead]:
            ins += 1
            i += 1
        elif bits[i:i + lookahead] == ref[j + 1:j + 1 + lookahead]:
            dele += 1
            j += 1
        else:
            sub += 1                    # isolated mismatch, no resync signature
            i += 1
            j += 1
    tail = min(len(bits) - i, len(ref) - j)     # no room left to resync
    sub += sum(1 for k in range(tail) if bits[i + k] != ref[j + k])
    return sub, ins, dele, j + tail


def align(bits, golden):
    """Offset of `bits` in `golden`, mismatches, how, and bits compared.

    The sync word pins the alignment when it survived the decode.  A clip
    that runs past the end of the frame decodes more bits than the golden
    holds from that offset; the overhang is reported and the comparison
    covers what does overlap, rather than throwing the alignment away.
    Without a sync word every offset is scored instead.
    """
    b = np.frombuffer(bits.encode(), np.uint8)
    g = np.frombuffer(golden.encode(), np.uint8)
    p, q = bits.find(SYNC), golden.find(SYNC)
    if p >= 0 and q >= 0 and q - p >= 0:
        off = q - p
        n = min(len(b), len(g) - off)
        if n > 0:
            return off, int((b[:n] != g[off:off + n]).sum()), "sync word", n
    if len(b) > len(g):
        sys.exit(f"decoded {len(b)} bits but the golden frame has {len(g)}, "
                 f"and no sync word to align on - wrong golden file, or the "
                 f"clip is far too wide")
    best = (len(bits) + 1, 0)
    for off in range(len(g) - len(b) + 1):
        e = int((b != g[off:off + len(b)]).sum())
        if e < best[0]:
            best = (e, off)
            if e == 0:
                break
    return best[1], best[0], "best fit", len(b)


def main():
    ap = argparse.ArgumentParser(
        description="Decode an FSK capture and compare it to a golden bit stream.")
    ap.add_argument("csv", help="waveform CSV")
    ap.add_argument("--start", type=float, required=True,
                    help="clip start time in seconds")
    ap.add_argument("--end", type=float, required=True,
                    help="clip end time in seconds")
    ap.add_argument("--symbol", type=float,
                    help="symbol period in microseconds (default %.4f)"
                         % (SYMBOL * 1e6))
    ap.add_argument("--golden", required=True,
                    help="golden bit stream from make_golden.py")
    ap.add_argument("-o", "--output", help="write the decoded bits here")
    ap.add_argument("--align-at", type=int, metavar="BIT",
                    help="align the decoded stream to this golden bit instead "
                         "of searching for the sync word, which does not "
                         "survive a badly degraded capture")
    ap.add_argument("--include-stage1", action="store_true",
                    help="also score the stage-1 preamble, which is normally "
                         "skipped as system start-up rather than link data")
    ap.add_argument("--scan", type=int, default=0, metavar="N",
                    help="report the error count in each block of N bits")
    ap.add_argument("--summary", metavar="FILE",
                    help="append '<bits> <errors> <capture>' to FILE, for a "
                         "caller totalling several captures")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit(f"{args.csv}: no such file")
    if args.end <= args.start:
        sys.exit("--end must be greater than --start")
    if args.symbol:
        globals()["SYMBOL"] = args.symbol * 1e-6

    rows, t, s = extract_edges(args.csv, args.start, args.end)
    n_edges = t.size
    t, s, removed = remove_glitch_edges(t, s, GLITCH_SPAN)
    runs = find_runs(t, s)
    if not runs:
        sys.exit("no edges inside the requested time window")
    counts = count_symbols(runs)
    bits = "".join(str(runs[i][0]) * n for i, n in enumerate(counts))

    print(f"{args.csv}  {args.start:.6f} .. {args.end:.6f} s "
          f"({(args.end - args.start) * 1e3:.3f} ms, {rows} samples)")
    print(f"  {n_edges} edges, {len(removed)} glitch edge(s) removed, "
          f"{len(runs)} windows -> {len(bits)} bits")

    golden = read_bits(args.golden)
    if args.align_at is not None:
        off, how = args.align_at, "--align-at"
        scored = min(len(bits), len(golden) - off)
        if scored <= 0:
            sys.exit(f"--align-at {off} leaves nothing to score")
        errors = 0
    else:
        off, errors, how, scored = align(bits, golden)
    overhang = len(bits) - scored
    bits, ref = bits[:scored], golden[off:off + scored]
    print(f"  aligned at golden bit {off} (by {how}); "
          f"sync word at decoded bit {bits.find(SYNC)}")
    if overhang:
        print(f"  {overhang} bit(s) decoded past the end of the frame, "
              f"not scored - trim --end")

    if not args.include_stage1:
        drop = max(0, stage1_length(golden) - off)
        if drop:
            print(f"  skipping {drop} stage-1 bit(s) (golden {off}-"
                  f"{off + drop - 1}): start-up, not link data")
            off += drop
            bits, ref, scored = bits[drop:], ref[drop:], scored - drop
            if scored <= 0:
                sys.exit("nothing left to score after the stage-1 preamble - "
                         "the clip ends before the data does")
    sub, ins, dele, scored = count_errors(bits, ref)
    errors = sub + ins + dele
    if ins or dele:
        print(f"  {ins} insertion(s), {dele} deletion(s) - a dropout swallowed "
              f"or added a symbol; counted once, not as loss of alignment")
    print(f"  {errors} bit errors in {scored} bits -> "
          + (f"BER {errors / scored:.3e}" if errors
             else f"BER < {1.0 / (2 * scored):.3e}  (1/2N)"))

    if args.scan:
        wrong = np.flatnonzero(np.frombuffer(bits.encode(), np.uint8) !=
                               np.frombuffer(ref.encode(), np.uint8))
        print(f"  errors per {args.scan} bits:")
        for a in range(0, len(bits), args.scan):
            n = int(((wrong >= a) & (wrong < a + args.scan)).sum())
            print(f"    {a:>6}-{min(a + args.scan, len(bits)):<6} {n:>5} "
                  + "#" * min(n, 50))

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(bits + "\n")
        print(f"  wrote {args.output}")
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(f"{scored}\t{errors}\t{os.path.basename(args.csv)}\n")
    # Bit errors are the measurement, not a failure of the run - only a
    # broken decode (handled above with sys.exit) is worth a non-zero code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
