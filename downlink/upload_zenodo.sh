#!/bin/sh
# Create a Zenodo draft record and upload the packed captures to it.
#
# Everything except publishing: the draft is created, the metadata from
# zenodo.json is applied, the files go up, and the reserved DOI is printed.
# Publishing is deliberately left out - a published record cannot be
# changed or deleted, only superseded, so it is a decision to make in the
# web interface after looking at the draft.
#
#     ./upload_zenodo.sh                       # new draft
#     ZENODO_DEPOSITION=22690538 ./upload_zenodo.sh    # into an existing one
#     ZENODO_HOST=sandbox.zenodo.org ./upload_zenodo.sh
#
# Uploading into an existing draft is how an interrupted run resumes:
# files already on the record are skipped, so nothing is sent twice and no
# second draft is left behind.
#
# The token is read from ~/.zenodo-token and is never printed.  Make one at
# zenodo.org under Applications, New personal access token, and tick
# deposit:write only - publishing lives in deposit:actions, so withholding
# that scope means this token cannot publish even by mistake.  Save it
# without putting it in the shell history:
#
#     read -rs tok && printf '%s' "$tok" > ~/.zenodo-token
#     chmod 600 ~/.zenodo-token && unset tok
#
# The sandbox is a separate site and needs its own separate token.
#
# One trap in zenodo.json: Zenodo drops a creator it cannot parse instead
# of rejecting it, so a record can come back with an affiliation and no
# author and look fine until someone reads it.  Names go in as
# "Family, Given", and an ORCID field is better left out than filled with
# a placeholder.
set -e

host=${ZENODO_HOST:-zenodo.org}
tokenfile=${ZENODO_TOKEN_FILE:-$HOME/.zenodo-token}
src=${1:-../captures-archive}

[ -r "$tokenfile" ] || { echo "no token at $tokenfile - see the notes at the top" >&2; exit 1; }
[ -d "$src" ] || { echo "$src not found - run ./pack_captures.sh first" >&2; exit 1; }
token=$(tr -d ' \t\n' < "$tokenfile")

api="https://$host/api/deposit/depositions"
auth="Authorization: Bearer $token"
body=$(mktemp)
trap 'rm -f "$body"' EXIT

# curl -f hides the response body, and Zenodo puts the reason a request was
# rejected in that body.  Capture the status separately and print what the
# server actually said before giving up.
#
# Zenodo's gateway returns 502 on a large upload often enough that a run
# without retries rarely finishes: the first attempt at this set died on
# the second file.  A 5xx or a dropped connection is the server having a
# moment and is retried; a 4xx is the request being wrong and is not.
call() {
    _try=1
    while :; do
        _code=$(curl -s -o "$body" -w '%{http_code}' -H "$auth" "$@") && _net=0 || _net=$?

        case $_code in
            2*) return 0 ;;
        esac

        if [ "$_try" -ge 5 ]; then
            echo "giving up after $_try attempts: HTTP $_code (curl $_net)" >&2
            head -c 2000 "$body" >&2
            echo >&2
            return 1
        fi

        case $_code in
            4*) echo "HTTP $_code from $host" >&2      # our fault, not theirs
                head -c 2000 "$body" >&2
                echo >&2
                return 1 ;;
        esac

        _wait=$((_try * 15))
        echo "  HTTP ${_code:-none} (curl $_net), retry $_try in ${_wait}s" >&2
        sleep "$_wait"
        _try=$((_try + 1))
    done
}

if [ -n "$ZENODO_DEPOSITION" ]; then
    echo "using existing draft $ZENODO_DEPOSITION on $host"
    call "$api/$ZENODO_DEPOSITION"
else
    echo "creating draft on $host"
    call -X POST "$api" -H "Content-Type: application/json" -d @zenodo.json
fi

eval "$(python3 - "$body" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1]))
doi = (d["metadata"].get("prereserve_doi") or {}).get("doi", "not reserved")
have = " ".join(f["filename"] for f in d.get("files", []))
for k, v in (("id", d["id"]), ("bucket", d["links"]["bucket"]),
             ("doi", doi), ("have", have)):
    print(f"{k}={shlex.quote(str(v))}")
PY
)"

echo "draft $id, reserved DOI $doi"
echo

# The bucket API takes files up to 50 GB each, unlike the 100 MB legacy
# endpoint, so the packed captures go up whole rather than in parts.
for f in "$src"/*; do
    name=$(basename "$f")
    case " $have " in
        *" $name "*) echo "have $name"; continue ;;
    esac
    echo "uploading $name"
    call -X PUT "$bucket/$name" --upload-file "$f"
done

echo
echo "draft ready at https://$host/uploads/$id"
echo "reserved DOI: $doi"
echo
echo "next: check the draft in the browser, then publish there."
echo "      put the record number in get_captures.sh."
