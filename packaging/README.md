# macOS application bundle

This directory documents a credential-free launcher scaffold. It wraps an existing Dedupe
checkout or installation; it does **not** embed Python or dependencies.

## Local build

Install Dedupe first (a checkout `.venv` is preferred), then run these commands from the
repository root on macOS:

```bash
scripts/build-macos-app.sh
open build/Dedupe.app
```

The generated bundle is under ignored `build/`. Its launcher tries, in order,
`DEDUPE_EXECUTABLE`, the selected checkout's `.venv/bin/dedupe`, `dedupe` on a conservative
Finder-safe `PATH`, and Python 3.11+ with the checkout's `src` directory. It invokes only
`dedupe ui`; it does not install packages or update the checkout. Rebuild the bundle if the
checkout moves. See `scripts/build-macos-app.sh --help` for output, repository, and icon options.

If `packaging/Dedupe.icns` (or one of the documented conventional asset paths) exists, it is
copied automatically. A PNG supplied with `--icon` is converted using macOS `sips` and
`iconutil`. With no source, the bundle safely uses the generic application icon.

For private local use, an ad-hoc signature can reduce signature-related launch ambiguity:

```bash
scripts/build-macos-app.sh --adhoc-sign
```

Ad-hoc signing provides no developer identity, trust, distribution, or notarization.

## Developer ID release and notarization

Public distribution requires an Apple Developer Program membership, a **Developer ID
Application** signing identity installed in the Keychain, the associated Team ID, Xcode
command-line tools, and an existing `notarytool` Keychain profile. Create that profile yourself
according to Apple's documentation; the scripts never create, discover, or store credentials.

For example, create and validate a profile directly with Apple before running the release helper:

```bash
xcrun notarytool store-credentials dedupe-notary \
  --apple-id 'you@example.com' --team-id 'TEAMID' --password 'APP-SPECIFIC-PASSWORD'
xcrun notarytool history --keychain-profile dedupe-notary
```

The first command writes notarization credentials to your login Keychain. Run it yourself only
on a trusted release machine; Dedupe's scripts never execute it.

The release helper refuses to proceed when any explicit prerequisite variable is missing and is
dry-run by default:

```bash
export MACOS_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)'
export APPLE_TEAM_ID='TEAMID'
export NOTARYTOOL_PROFILE='dedupe-notary'
scripts/release-macos-app.sh --app build/Dedupe.app       # validate/print only
scripts/release-macos-app.sh --app build/Dedupe.app --execute
```

`--execute` signs with the hardened runtime and secure timestamp, creates a temporary ZIP next to
the app, submits it to Apple's notarization service, waits, removes the ZIP, staples, and verifies.
It therefore performs network and Keychain-backed
operations; do not use it until credentials and release policy are ready. For distribution, a
signed DMG or ZIP and release-specific versioning may also be desirable.

This scaffold is **not Mac App Store packaging**. It does not implement App Sandbox,
entitlements, provisioning profiles, embedded runtimes, receipt handling, or App Store Connect
submission.
