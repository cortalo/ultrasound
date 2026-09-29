#!/bin/sh
# Compress the raw data for upload to the data archive.
#
# The files are ASCII and compress about 5x, which takes the set from
# 374 MB to roughly 70 MB.  xz is slow to write and fast to read, which is
# the right way round for something written once and downloaded many times.
#
#     ./pack_captures.sh [outdir]        # default: ../uplink-archive
#
# The 100 bit-decision parts go into one tarball, because a Zenodo record
# holds at most 100 files; the two scope chunks go up as <name>.csv.xz.
# SHA256SUMS.xz covers the compressed files.  SHA256SUMS in this directory
# covers the uncompressed originals, so the unpacked files can be checked
# against it after download.
set -e

out=${1:-../uplink-archive}
mkdir -p "$out"

while read -r sum f; do
    [ -e "$f" ] || { echo "$f missing"; exit 1; }
done < SHA256SUMS

if [ -e "$out/BER_bit_decisions.tar.xz" ]; then
    echo "skip BER_bit_decisions.tar.xz (already packed)"
else
    echo "packing BER_bit_decisions.tar.xz"
    COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -cf - BER_bit_decisions_part_*.csv \
        | xz -6 -T0 > "$out/BER_bit_decisions.tar.xz.part"
    mv "$out/BER_bit_decisions.tar.xz.part" "$out/BER_bit_decisions.tar.xz"
fi

for f in scope_ch4_*_chunk.csv; do
    if [ -e "$out/$f.xz" ]; then
        echo "skip $f (already packed)"
        continue
    fi
    echo "packing $f"
    xz -6 -T0 -c "$f" > "$out/$f.xz.part"
    mv "$out/$f.xz.part" "$out/$f.xz"
done

( cd "$out" && shasum -a 256 *.xz > SHA256SUMS.xz )
echo
du -sh "$out"
echo "upload the contents of $out as one archive record"
