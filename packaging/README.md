# macOS application bundle

Dedupe can be built as a real, double-clickable macOS application. The bundle contains:

- a native AppKit shell and WKWebView window with normal macOS menus, window behavior, copy/paste, fullscreen, and Cmd+Q
- an embedded Python backend produced with PyInstaller, so neither a checkout nor a separate `dedupe` installation is required
- the web review interface served only on loopback inside that native window
- the OpenCV person/face detector dependencies by default
- an application icon and an ad-hoc local signature

The backend is the same Dedupe review application as `dedupe ui`; the native shell owns its lifecycle instead of a browser tab or Terminal window.

## Build on macOS

From the repository root on macOS:

```bash
scripts/build-macos-app.sh
open build/Dedupe.app
```

The build requires macOS 12+, Python 3.11+, Xcode command-line tools (`swiftc`, `sips`, `iconutil`), and network access for Python packages. It creates an isolated build environment under ignored `build/`, embeds the backend, compiles the Swift shell, smoke-tests that the embedded server starts and shuts down, signs the app ad hoc, and leaves `build/Dedupe.app` ready to open.

Useful variants:

```bash
scripts/build-macos-app.sh --zip build/Dedupe-macOS.zip
scripts/build-macos-app.sh --python /opt/homebrew/bin/python3.13
scripts/build-macos-app.sh --no-human       # smaller app without OpenCV detectors
scripts/build-macos-app.sh --keep-work      # debug a PyInstaller failure
```

The resulting app is self-contained for Python and Dedupe's Python dependencies. It does **not** embed ffmpeg/ffprobe. Video similarity and video thumbnails still require ffmpeg, which the app finds in `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, or the system `PATH` when launched from Finder:

```bash
brew install ffmpeg
```

The optional Photon/Moondream backend and external DigUp integration are not embedded either; they remain opt-in external installations. Dedupe's OpenCV YuNet and YOLOX model files are bundled.

## Sharing an unsigned build

The default ad-hoc signature lets the app run on Apple Silicon and reduces signature ambiguity, but it is not Developer ID trust. If macOS quarantines a zipped copy downloaded from the internet, open it once with **right-click → Open** and confirm the unidentified-developer prompt, or build locally from source. Public distribution should use the signed/notarized release flow below.

## Icon

`packaging/Dedupe.png` is the committed 1024×1024 source icon. It was generated from `packaging/generate-icon.py`; after editing that script, regenerate it with Pillow available:

```bash
.venv/bin/python packaging/generate-icon.py
```

The build converts it to `Dedupe.icns` with the macOS `sips` and `iconutil` tools.

## Developer ID release and notarization

Public distribution requires an Apple Developer Program membership, a **Developer ID Application** signing identity installed in the Keychain, the associated Team ID, Xcode command-line tools, and an existing `notarytool` Keychain profile. Create that profile yourself according to Apple's documentation; the scripts never create, discover, or store credentials.

For example, create and validate a profile directly with Apple before running the release helper:

```bash
xcrun notarytool store-credentials dedupe-notary \
  --apple-id 'you@example.com' --team-id 'TEAMID' --password 'APP-SPECIFIC-PASSWORD'
xcrun notarytool history --keychain-profile dedupe-notary
```

The first command writes notarization credentials to your login Keychain. Run it yourself only on a trusted release machine; Dedupe's scripts never execute it.

The release helper refuses to proceed when any explicit prerequisite variable is missing and is dry-run by default:

```bash
export MACOS_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)'
export APPLE_TEAM_ID='TEAMID'
export NOTARYTOOL_PROFILE='dedupe-notary'
scripts/release-macos-app.sh --app build/Dedupe.app       # validate/print only
scripts/release-macos-app.sh --app build/Dedupe.app --execute
```

`--execute` signs the bundle and embedded binaries with the hardened runtime and secure timestamp, creates a temporary ZIP next to the app, submits it to Apple's notarization service, waits, removes the ZIP, staples, and verifies. It therefore performs network and Keychain-backed operations; do not use it until credentials and release policy are ready. For distribution, a signed DMG or ZIP and release-specific versioning may also be desirable.

This scaffold is **not Mac App Store packaging**. It does not implement App Sandbox, entitlements, provisioning profiles, receipt handling, or App Store Connect submission.
