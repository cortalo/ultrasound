#!/bin/sh
# Compress the raw captures for upload to the data archive.
#
# The captures are ASCII scope dumps and compress about 8x, which takes the
# whole set from 4.9 GB to roughly 600 MB.  xz is slow to write and fast to
# read, which is the right way round for something written once and
# downloaded many times.
#
#     ./pack_captures.sh [outdir]        # default: ../captures-archive
#
# Writes <name>.csv.xz per capture plus SHA256SUMS.xz over the compressed
# files.  SHA256SUMS in this directory covers the uncompressed originals, so
# `xz -d` output can be checked against it after download.
set -e

out=${1:-../captures-archive}
mkdir -p "$out"

for f in *.csv; do
    case $f in edges_*) continue ;; esac      # derived, not a capture
    [ -e "$f" ] || { echo "no captures here"; exit 1; }
    if [ -e "$out/$f.xz" ]; then
        echo "skip $f (already packed)"
        continue
    fi
    echo "packing $f"
    xz -6 -T0 -c "$f" > "$out/$f.xz.part"
    mv "$out/$f.xz.part" "$out/$f.xz"
done

( cd "$out" && shasum -a 256 *.csv.xz > SHA256SUMS.xz )
echo
du -sh "$out"
echo "upload the contents of $out as one archive record"
