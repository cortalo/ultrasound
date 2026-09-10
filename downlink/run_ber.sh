#!/bin/sh
# Decode every capture at every symbol rate and score it against its golden
# bit stream.
#
#   ./run_ber.sh              every capture, every rate
#   ./run_ber.sh 5000_50k     only captures whose name matches
#   ./run_ber.sh 80k          only the 80 kbaud rate
#
# Captures that are not present are skipped, so a partial download still
# scores the rest.  Exits non-zero if a capture is present but cannot be
# decoded; bit errors are the measurement, not a failure.
#
# Clip bounds come from SYN alone, never from where the bit errors are -
# clipping to the largest error-free span would make a zero BER a foregone
# conclusion rather than a measurement.
#
#   start  the first 1 ms block where SYN is modulating, i.e. its period
#          swings between the two tones instead of sitting at the 856.8 kHz
#          idle.  This is always before the PRBS payload begins, so the
#          whole payload is covered.
#   end    whichever comes first:
#            - a single SYN cycle longer than 1.5x the median (1.16 us).
#              The PLL has slipped; FREQ_UP_2 dies within ~20 us of it and
#              everything after is a failed link, not data.
#            - SYN modulation collapsing back to idle, which is the end of
#              the payload and the end of the frame.
#
# Bit errors inside those bounds are real and are meant to be counted.  The
# stage-1 preamble is skipped by ber.py: its errors are the PLL acquiring at
# start-up, not link errors, and its long 100 us runs decode too easily to
# belong in the same average as payload data.
#
# Only the symbol period changes between rates.  The midspan decode and its
# 0.85 / 0.15 rounding thresholds sit on a plateau common to every rate
# here, so nothing else is retuned.
cd "$(dirname "$0")" || exit 1

match=${1:-}
fail=0
sum=$(mktemp) || exit 1
trap 'rm -f "$sum"' EXIT INT TERM

golden() {
    [ -f "$2" ] || ./make_golden.py "$1" -o "$2" >/dev/null || exit 1
}

# The optional sixth argument is a golden bit offset to align on.  The
# faster rates degrade far enough that the 32-bit sync word does not
# survive the decode, and without it ber.py would fall back to scoring
# every offset and pick whichever fits best - which is not a measurement.
score() {           # csv symbol_us start end golden [align_at]
    case "$1$rate" in
        *"$match"*) ;;
        *) return 0 ;;
    esac
    [ -f "$1" ] || { echo "skip $1 (not found)"; return 0; }
    align=""
    [ -n "$6" ] && align="--align-at $6"
    ./ber.py "$1" --symbol "$2" --start "$3" --end "$4" --golden "$5" \
             $align -o "bits_${1%.csv}.txt" --summary "$sum" || fail=1
    echo
}

total() {           # heading
    [ -s "$sum" ] || { : > "$sum"; return 0; }
    echo "  $1"
    awk -F'\t' '
        { bits += $1; errs += $2
          printf "  %-16s %7d bits  %5d errors\n", $3, $1, $2 }
        END {
            print  "  ----------------------------------------------"
            printf "  %-16s %7d bits  %5d errors  ", "TOTAL", bits, errs
            if (errs > 0) printf "BER = %.3e\n", errs / bits
            else          printf "BER < %.3e  (1/2N)\n", 1 / (2 * bits)
        }' "$sum"
    echo
    : > "$sum"
}

golden 2368_50k.txt  golden_2368.txt
golden 5000_50k.txt  golden_5000.txt
golden 10000_50k.txt golden_10000.txt

# 20 us / 50 kbaud.  The first three end on a long SYN cycle, the PLL
# slipping mid-frame; the rest run their frame to completion.
rate=50k
score 2368_50k.csv    20 3.817841 3.835237 golden_2368.txt
score 5000_50k.csv    20 4.185476 4.254569 golden_5000.txt
score 5000_50k_1.csv  20 5.442340 5.510858 golden_5000.txt
score 5000_50k_2.csv  20 4.305234 4.459234 golden_5000.txt
score 5000_50k_3.csv  20 4.275698 4.428698 golden_5000.txt
score 5000_50k_4.csv  20 4.257639 4.410639 golden_5000.txt
score 5000_50k_5.csv  20 5.530186 5.684186 golden_5000.txt
score 5000_50k_6.csv  20 4.495288 4.648288 golden_5000.txt
score 5000_50k_7.csv  20 4.554238 4.708238 golden_5000.txt
score 10000_50k.csv   20 6.672030 6.926030 golden_10000.txt
score 10000_50k_1.csv 20 5.600712 5.853712 golden_10000.txt
total "50 kbaud, 20 us"

rate=60k
score 10000_60k.csv 16.6667 4.095097 4.306097 golden_10000.txt
total "60 kbaud, 16.667 us"

rate=67k
score 10000_67k.csv 15 4.939356 5.130356 golden_10000.txt
total "66.7 kbaud, 15 us"

rate=70k
score 10000_70k.csv 14.2857 3.672420 3.817617 golden_10000.txt 2500
total "70 kbaud, 14.286 us"

rate=80k
score 10000_80k.csv 12.5 3.490687 3.617666 golden_10000.txt 2500
total "80 kbaud, 12.5 us"

if [ "$fail" -ne 0 ]; then
    echo "warning: at least one capture could not be decoded"
fi
exit "$fail"
