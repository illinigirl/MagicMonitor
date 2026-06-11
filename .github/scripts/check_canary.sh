#!/usr/bin/env bash
# Canary assertion logic for one park page's HTML.
#
# Usage: check_canary.sh "<html>" "<label>"
# Exit 0 = healthy (meta line found, attractions count > 0).
# Exit 1 = unhealthy (count is 0, or the meta line couldn't be parsed).
#
# Lives in its own file so the canary workflow's self-test exercises
# the EXACT same code that runs against the live site — otherwise the
# assertion can silently rot (as it did before 2026-06-11, when React
# SSR "<!-- -->" separators made the old grep un-matchable and the
# stop-loss became dead code).
#
# The live meta line renders (with SSR separators) as:
#   35<!-- --> attractions · <!-- -->30<!-- --> open · ...
# We strip the separators, then make a POSITIVE assertion: the
# "<N> attractions" count must be parseable AND greater than zero.
# A positive assertion fails loudly if the markup changes, rather
# than a negative match that passes by default.
set -euo pipefail

html="${1:-}"
label="${2:-page}"

# Strip the React SSR comment separators so adjacent text nodes
# rejoin into "35 attractions · 30 open · ...".
clean="${html//<!-- -->/}"

# Pull the integer immediately preceding " attractions".
count="$(printf '%s' "$clean" \
  | grep -oE '[0-9]+ attractions' \
  | head -1 \
  | grep -oE '^[0-9]+' || true)"

if [ -z "$count" ]; then
  # No parseable count: the meta line is missing or its structure
  # changed. Fail loudly — never assume healthy.
  echo "::error::$label: could not parse attractions count (meta line missing or markup changed)"
  exit 1
fi

if [ "$count" -eq 0 ]; then
  echo "::error::$label: rendered 0 attractions (empty SSR read — the 2026-05-24 regression shape)"
  exit 1
fi

echo "$label: $count attractions"
exit 0
