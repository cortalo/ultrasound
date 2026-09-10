#!/bin/sh
# Download the raw captures from the archive record and verify them.
#
# The captures are too large for git and live in a Zenodo record instead.
# Zenodo file URLs are stable for the life of the record, so fetching them
# needs nothing but curl and the record number:
#
#     ./get_captures.sh                 # all fifteen captures, 4.9 GB
#     ./get_captures.sh 5000_50k_2      # just the ones whose name matches
#
# Files already present and already correct are skipped, so an interrupted
# run resumes by being run again.
set -e

record=${ZENODO_RECORD:-22690538}
base="https://zenodo.org/records/$record/files"
match=${1:-}

# SHA256SUMS is the list of captures as well as the check on them.
while read -r sum name; do
    case $name in *"$match"*) ;; *) continue ;; esac

    if [ -e "$name" ] && [ "$(shasum -a 256 "$name" | cut -d' ' -f1)" = "$sum" ]; then
        echo "have $name"
        continue
    fi

    echo "fetching $name"
    curl -fL --progress-bar -o "$name.xz.part" "$base/$name.xz?download=1"
    mv "$name.xz.part" "$name.xz"
    xz -d "$name.xz"

    got=$(shasum -a 256 "$name" | cut -d' ' -f1)
    if [ "$got" != "$sum" ]; then
        echo "$name: checksum mismatch, download is corrupt" >&2
        exit 1
    fi
done < SHA256SUMS

echo
echo "all captures present and verified"
