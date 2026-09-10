#!/usr/bin/env python3
"""Build the golden bit stream for a capture from its spec .txt.

The spec files describe one frame:

    stage 1   2500 bits of 100-us levels, five 0s then five 1s repeated
    stage 2   100 alternating training bits, a 32-bit sync word
              (0x1ACFFC1D), then the PRBS15 payload

Some spec files list every part in its own [SECTION]; others carry only
the payload and leave the rest to the documented structure.  Either way
this writes the whole frame as one line of 0/1 characters, which is what
ber.py compares a decoded capture against.

    ./make_golden.py 5000_50k.txt -o golden_5000.txt
"""

import argparse
import re
import sys

STAGE1_CELL = "0000011111"          # five 0s, five 1s - one 100 us level pair
STAGE1_BITS = 2500
TRAINING_BITS = 100
SYNC_WORD = 0x1ACFFC1D
SYNC_BITS = 32


def read_sections(path):
    """Return {SECTION: bitstring} for every [SECTION] in the file."""
    out, name = {}, None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                name = line[1:-1]
                out[name] = []
            elif name and line and not line.startswith("#"):
                if not re.fullmatch(r"[01]+", line):
                    name = None                 # not a bit section after all
                    continue
                out[name].append(line)
    return {k: "".join(v) for k, v in out.items() if v}


def main():
    ap = argparse.ArgumentParser(
        description="Write a capture's golden frame as 0/1 text.")
    ap.add_argument("spec", help="the capture's .txt spec file")
    ap.add_argument("-o", "--output", required=True, help="output text file")
    ap.add_argument("--wrap", type=int, default=0,
                    help="wrap every N bits (0 = one long line)")
    args = ap.parse_args()

    sec = read_sections(args.spec)
    if not sec:
        sys.exit(f"{args.spec}: no [SECTION] of bits found")

    def pick(*names):
        for n in names:
            if n in sec:
                return sec[n], n
        return None, None

    stage1, s1_from = pick("STAGE1_100US_CELL_BITS", "STAGE1_BITS")
    if stage1 is None:
        stage1, s1_from = STAGE1_CELL * (STAGE1_BITS // len(STAGE1_CELL)), "structure"
    train, tr_from = pick("STAGE2_TRAINING_BITS", "TRAINING_BITS")
    if train is None:
        train, tr_from = "01" * (TRAINING_BITS // 2), "structure"
    sync, sy_from = pick("STAGE2_SYNC_BITS", "SYNC_BITS")
    if sync is None:
        sync, sy_from = format(SYNC_WORD, f"0{SYNC_BITS}b"), "structure"
    payload, pl_from = pick("STAGE2_PAYLOAD_BITS", "PRBS15_PAYLOAD_BITS")
    if payload is None:
        sys.exit(f"{args.spec}: no payload section found "
                 f"(have: {', '.join(sec)})")

    for label, bits, src in (("stage 1 ", stage1, s1_from),
                             ("training", train, tr_from),
                             ("sync    ", sync, sy_from),
                             ("payload ", payload, pl_from)):
        print(f"  {label} {len(bits):>5} bits  (from {src})")

    frame = stage1 + train + sync + payload
    with open(args.output, "w") as fh:
        if args.wrap > 0:
            for i in range(0, len(frame), args.wrap):
                fh.write(frame[i:i + args.wrap] + "\n")
        else:
            fh.write(frame + "\n")
    print(f"{len(frame)} bits -> {args.output}")


if __name__ == "__main__":
    main()
