#!/usr/bin/env bash
# Explicit Developer ID signing/notarization release step; never runs implicitly.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/release-macos-app.sh --app PATH [--execute]

Validate prerequisites for Developer ID signing and Apple notarization. Without
--execute, prints the planned commands and changes nothing. --execute requires:

  MACOS_SIGN_IDENTITY  Developer ID Application identity name or SHA-1
  APPLE_TEAM_ID        Apple Developer Team ID
  NOTARYTOOL_PROFILE   Existing notarytool Keychain profile name

This script does not create credentials or modify the Keychain.
EOF
}
die() { printf 'release-macos-app: %s\n' "$*" >&2; exit 1; }

APP=""
EXECUTE=0
while (($#)); do
  case "$1" in
    --app) (($# >= 2)) || die '--app requires a path'; APP="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ -n "$APP" ]] || die '--app is required'
[[ -d "$APP/Contents" ]] || die "app bundle not found: $APP"

missing=()
[[ -n "${MACOS_SIGN_IDENTITY:-}" ]] || missing+=(MACOS_SIGN_IDENTITY)
[[ -n "${APPLE_TEAM_ID:-}" ]] || missing+=(APPLE_TEAM_ID)
[[ -n "${NOTARYTOOL_PROFILE:-}" ]] || missing+=(NOTARYTOOL_PROFILE)
((${#missing[@]} == 0)) || die "required release credentials are absent: ${missing[*]} (see --help)"
command -v codesign >/dev/null 2>&1 || die 'codesign is unavailable (install Xcode command-line tools)'
command -v xcrun >/dev/null 2>&1 || die 'xcrun is unavailable (install Xcode command-line tools)'

printf 'Plan:\n'
printf '  codesign Developer ID bundle: %q\n' "$APP"
printf '  archive the signed bundle for notarization\n'
printf '  submit with notarytool profile: %q (team %q)\n' "$NOTARYTOOL_PROFILE" "$APPLE_TEAM_ID"
printf '  staple and verify the accepted ticket\n'
((EXECUTE)) || { printf 'Dry run only; pass --execute to perform these operations.\n'; exit 0; }

codesign --force --options runtime --timestamp --sign "$MACOS_SIGN_IDENTITY" "$APP"
ARCHIVE="${APP%.app}-notarization.zip"
trap 'rm -f "$ARCHIVE"' EXIT
/usr/bin/ditto -c -k --keepParent "$APP" "$ARCHIVE"
xcrun notarytool submit "$ARCHIVE" --keychain-profile "$NOTARYTOOL_PROFILE" --team-id "$APPLE_TEAM_ID" --wait
rm -f "$ARCHIVE"
trap - EXIT
xcrun stapler staple "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
xcrun stapler validate "$APP"
