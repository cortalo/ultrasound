#!/bin/sh
# Download the raw data from the archive record and verify it.
#
# The 100 bit-decision parts and the two scope chunks are too large for git
# and live in a Zenodo record instead: the parts as one tarball, since a
# record holds at most 100 files, and each scope chunk on its own.  Zenodo
# file URLs are stable for the life of the record, so fetching them needs
# nothing but curl and the record number:
#
#     ./get_captures.sh                 # everything, 68 MB down, 374 MB unpacked
#     ./get_captures.sh scope_ch4       # just the archives whose name matches
#
# Archives whose contents are already present and correct are skipped, so
# an interrupted run resumes by being run again.
set -e

record=${ZENODO_RECORD:-23036359}
base="https://zenodo.org/records/$record/files"
match=${1:-}

# SHA256SUMS lists the unpacked files; check the ones a pattern names.
verified() {
    grep "$1" SHA256SUMS | shasum -a 256 -c --quiet >/dev/null 2>&1
}

for a in BER_bit_decisions.tar.xz scope_ch4_first_chunk.csv.xz scope_ch4_last_chunk.csv.xz; do
    case $a in *"$match"*) ;; *) continue ;; esac

    case $a in
        *.tar.xz) want=BER_bit_decisions_part_ ;;
        *)        want=${a%.xz} ;;
    esac

    if verified "$want"; then
        echo "have $a"
        continue
    fi

    echo "fetching $a"
    curl -fL --progress-bar -o "$a.part" "$base/$a?download=1"
    mv "$a.part" "$a"
    case $a in
        *.tar.xz) tar -xJf "$a" && rm "$a" ;;
        *)        xz -df "$a" ;;
    esac

    if ! verified "$want"; then
        echo "$a: checksum mismatch, download is corrupt" >&2
        exit 1
    fi
done

echo
echo "all files present and verified"
