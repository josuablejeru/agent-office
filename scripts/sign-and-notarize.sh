#!/usr/bin/env bash
# Signs "Agent Office.app" for distribution and, if notarization credentials are
# given, has Apple notarize it and staples the ticket. Used by the release
# workflow and usable by hand.
#
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_KEY_PATH=AuthKey_XXXX.p8 NOTARY_KEY_ID=XXXX NOTARY_ISSUER_ID=uuid \
#   scripts/sign-and-notarize.sh "dist/Agent Office.app"
#
# Without the NOTARY_* variables the app is signed only. A downloaded copy opens
# without warnings only when it is signed with a Developer ID certificate AND
# notarized.
set -euo pipefail

APP="${1:?usage: sign-and-notarize.sh path/to/App.app}"
: "${SIGN_IDENTITY:?set SIGN_IDENTITY to the name of a signing certificate in the keychain}"
ENTITLEMENTS="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"
KEYCHAIN_ARGS=()
[ -n "${SIGN_KEYCHAIN:-}" ] && KEYCHAIN_ARGS=(--keychain "$SIGN_KEYCHAIN")

sign() {
  codesign --force --timestamp --options runtime "${KEYCHAIN_ARGS[@]}" --sign "$SIGN_IDENTITY" "$@"
}

echo "==> Signing the code inside the bundle"
# Inside out: every binary and library first, the app itself last. (--deep is
# not used: it is unreliable for bundles with code under Resources.)
count=0
while IFS= read -r -d '' file; do
  if file -b "$file" | grep -q "Mach-O"; then
    sign --entitlements "$ENTITLEMENTS" "$file" 2>&1 | grep -v ': replacing existing signature$' || true
    count=$((count + 1))
  fi
done < <(find "$APP/Contents/Resources" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0)
echo "    $count binaries and libraries"

echo "==> Signing the app"
sign --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --strict --verbose=2 "$APP" 2>&1 | tail -2

if [ -z "${NOTARY_KEY_PATH:-}" ]; then
  echo "==> Not notarized (no NOTARY_KEY_PATH given)"
  exit 0
fi
: "${NOTARY_KEY_ID:?set NOTARY_KEY_ID}" "${NOTARY_ISSUER_ID:?set NOTARY_ISSUER_ID}"

echo "==> Notarizing (this takes a few minutes)"
UPLOAD="$(mktemp -d)/upload.zip"
ditto -c -k --keepParent "$APP" "$UPLOAD"
xcrun notarytool submit "$UPLOAD" --wait \
  --key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID"
rm -f "$UPLOAD"
xcrun stapler staple "$APP"
spctl --assess --type execute --verbose=2 "$APP" 2>&1 | tail -2
echo "==> Signed and notarized"
