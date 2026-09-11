#!/usr/bin/env bash
# Fail when a file in this repo carries a term borrowed from the private repo
# this tooling was written beside.
#
# This repo is public. Its fixtures and its docstrings were written next to a
# private one, and a fixture in a sibling project once shipped a sentence lifted
# verbatim from it. Copy-paste is the whole failure mode: nobody decides to leak
# a name, they paste a realistic example and the name rides along.
#
# The forbidden terms are themselves the thing being protected, so the list is
# stored base64-encoded. A public file that spells out the words it forbids
# leaks exactly what it exists to hide, and it hands a search engine the list.
# Decode it locally to read it:
#
#     grep -oE "_B64='[^']*'" scripts/fixture-scrub.sh | cut -d\' -f2 | base64 -d
#
# For the same reason the failure output prints file:line and never the matching
# line. This repo's CI logs are public; echoing the hit would publish the term a
# second time, in a place nobody thinks to scrub.
#
# Usage: scripts/fixture-scrub.sh [DIR]     (default: the repo root)
# Exit 0 clean, 1 a term is present, 2 the check could not run.
set -euo pipefail

# Two lists, because the terms need two match rules. SUB is matched anywhere in
# a line: the org name appears glued inside a longer identifier, so a word
# boundary would miss it. WORD is matched with `grep -w`, because each of those
# terms is an ordinary English word this repo has no legitimate use for, and
# without the boundary they fire inside `rerouter` and `re-tenanted`.
SUB_B64='cHJpem1hbHxiaWZyb3N0fGNvbmZpZ1sgLV1hcGl8c3dpdGNoWyAtXWtleXxwcm92aWRlciBrZXl8cm91dGluZyBldmVudHxtYW5hZ2VtZW50IGtleXxQUkktWzAtOV17Myx9fGJpbGxhYmxlIGxlZ3xzZXR0bGUgd29ya2VyfGNpcmN1aXQgP2JyZWFrZXJ8a2luZGNyZWRpdHxwcmVsbG1ob29r'
WORD_B64='cm91dGVyfHJvdXRlcnN8dGVuYW50fHRlbmFudHN8YWR2aXNvcnxhZHZpc29yc3xkaWFsZWN0fGRpYWxlY3Rz'

# openssl is on every runner and every mac and takes the same flags on both.
# `base64 -d` is GNU and `base64 -D` is BSD, so neither spelling is portable.
decode() {
  if command -v openssl >/dev/null 2>&1; then printf '%s' "$1" | openssl base64 -d -A
  else printf '%s' "$1" | { base64 -d 2>/dev/null || base64 -D; }
  fi
}
SUB=$(decode "$SUB_B64") || SUB=
WORD=$(decode "$WORD_B64") || WORD=
if [ -z "$SUB" ] || [ -z "$WORD" ]; then
  echo "fixture-scrub: could not decode the term lists; no verdict is safe." >&2
  exit 2
fi

if [ $# -gt 0 ]; then
  ROOT=$1
elif ! ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  echo "fixture-scrub: not inside a git repo and no directory given." >&2
  exit 2
fi
[ -d "$ROOT" ] || { echo "fixture-scrub: $ROOT is not a directory." >&2; exit 2; }

# grep exits 1 on no matches, which is the passing case here, so the status is
# read deliberately instead of ending the script under errexit.
scan() {
  # grep exits 1 on no matches, which is the passing case here, so the status is
  # read deliberately instead of ending the run under errexit. It is also why
  # this is never piped straight into another command: the pipe would report the
  # tail's status and a real grep failure would read as clean.
  set +e
  out=$(grep -rniE "$@" \
    --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=styles \
    --binary-files=without-match "$ROOT" 2>/dev/null)
  status=$?
  set -e
  if [ "$status" -gt 1 ]; then
    echo "fixture-scrub: grep failed with status $status; no verdict is safe." >&2
    exit 2
  fi
  printf '%s' "$out" | cut -d: -f1,2
}
HITS=$(printf '%s\n%s' "$(scan "$SUB")" "$(scan -w "$WORD")" | grep -v '^$' | sort -u || true)

if [ -z "$HITS" ]; then
  echo "fixture-scrub: clean."
  exit 0
fi

echo "fixture-scrub: a borrowed term is present at these locations." >&2
while IFS= read -r hit; do
  printf '  %s\n' "${hit#"$ROOT"/}" >&2
done <<< "$HITS"
cat >&2 <<'MSG'

The matching text is deliberately not printed: this repo's CI logs are public,
so echoing it would publish the term again. Decode the list in this script and
grep locally to see what matched.

Rewrite the example with neutral prose. A fixture only has to be realistic
markdown, never a real sentence from a real system:

    The parser reads the schema's version from the header.

Do not paste the matching line into an issue, a pull request, or a commit
message while fixing it.
MSG
exit 1
