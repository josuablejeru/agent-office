#!/usr/bin/env bash
# Has Apple notarize a signed "Agent Office.app" and staples the ticket to it.
# Called by sign-and-notarize.sh, and on its own by the release workflow's
# Notarize job.
#
#   NOTARY_KEY_PATH=AuthKey_XXXX.p8 NOTARY_KEY_ID=XXXX NOTARY_ISSUER_ID=uuid \
#   scripts/notarize.sh "dist/Agent Office.app"
set -euo pipefail

APP="${1:?usage: scripts/notarize.sh path/to/Agent Office.app}"
: "${NOTARY_KEY_PATH:?set NOTARY_KEY_PATH}"
: "${NOTARY_KEY_ID:?set NOTARY_KEY_ID}" "${NOTARY_ISSUER_ID:?set NOTARY_ISSUER_ID}"

echo "==> Notarizing (this takes a few minutes)"
UPLOAD="$(mktemp -d)/upload.zip"
ditto -c -k --keepParent "$APP" "$UPLOAD"
AUTH=(--key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID")
RESULT="$(xcrun notarytool submit "$UPLOAD" --wait --output-format json "${AUTH[@]}")" || true
rm -f "$UPLOAD"
echo "$RESULT"
ID="$(printf '%s' "$RESULT" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
STATUS="$(printf '%s' "$RESULT" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
if [ "$STATUS" != "Accepted" ]; then
  # Apple's reasons: which files, and what is wrong with each.
  [ -n "$ID" ] && xcrun notarytool log "$ID" "${AUTH[@]}" || true
  echo "error: Apple did not accept the app (status: ${STATUS:-unknown})" >&2
  exit 1
fi
# Fetching the ticket from Apple occasionally drops the connection: try again.
for attempt in 1 2 3 4 5; do
  xcrun stapler staple "$APP" && break
  [ "$attempt" = 5 ] && { echo "error: stapling failed" >&2; exit 1; }
  echo "    stapling failed; trying again in $((attempt * 15))s"
  sleep $((attempt * 15))
done
spctl --assess --type execute --verbose=2 "$APP" 2>&1 | tail -2
echo "==> Signed and notarized"
