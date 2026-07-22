#!/usr/bin/env bash
# Build a small macOS launcher bundle for an existing Dedupe checkout/install.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build-macos-app.sh [options]

Build an unsigned Dedupe.app launcher bundle (default: build/Dedupe.app).

Options:
  --output PATH       Bundle output path
  --repo PATH         Dedupe checkout used by the launcher (default: this repo)
  --icon PATH         .icns or PNG icon source
  --adhoc-sign        Apply a local ad-hoc signature after building
  -h, --help          Show this help

At launch, DEDUPE_EXECUTABLE may override executable discovery. Otherwise the
bundle uses REPO/.venv/bin/dedupe, a dedupe command on PATH, or REPO/src.
EOF
}

die() { printf 'build-macos-app: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO="$DEFAULT_REPO"
OUTPUT="$DEFAULT_REPO/build/Dedupe.app"
ICON=""
ADHOC_SIGN=0

while (($#)); do
  case "$1" in
    --output) (($# >= 2)) || die '--output requires a path'; OUTPUT="$2"; shift 2 ;;
    --repo) (($# >= 2)) || die '--repo requires a path'; REPO="$2"; shift 2 ;;
    --icon) (($# >= 2)) || die '--icon requires a path'; ICON="$2"; shift 2 ;;
    --adhoc-sign) ADHOC_SIGN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ "$(uname -s)" == Darwin ]] || die 'macOS is required'
[[ -d "$REPO/src/dedupe" || -x "$REPO/.venv/bin/dedupe" ]] \
  || die "not a Dedupe checkout/install: $REPO"
REPO="$(cd "$REPO" && pwd)"
[[ "$OUTPUT" == *.app ]] || die '--output must end in .app'

if [[ -e "$OUTPUT" ]]; then
  [[ -d "$OUTPUT/Contents" ]] || die "refusing to replace non-bundle path: $OUTPUT"
  rm -rf "$OUTPUT"
fi

CONTENTS="$OUTPUT/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
mkdir -p "$MACOS" "$RESOURCES"
printf '%s\n' "$REPO" > "$RESOURCES/repository-path"

cat > "$CONTENTS/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Dedupe</string>
  <key>CFBundleDisplayName</key><string>Dedupe</string>
  <key>CFBundleIdentifier</key><string>com.sethsaler.dedupe</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Dedupe</string>
  <key>CFBundleIconFile</key><string>Dedupe.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><false/>
</dict></plist>
EOF

cat > "$MACOS/Dedupe" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

alert() {
  local message="$1"
  printf 'Dedupe: %s\n' "$message" >&2
  /usr/bin/osascript -e 'on run argv' -e 'display alert "Dedupe" message (item 1 of argv) as critical' -e 'end run' "$message" >/dev/null 2>&1 || true
}

CONTENTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cat "$CONTENTS/Resources/repository-path" 2>/dev/null || true)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ -n "${DEDUPE_EXECUTABLE:-}" ]]; then
  [[ -x "$DEDUPE_EXECUTABLE" ]] || { alert "DEDUPE_EXECUTABLE is not executable: $DEDUPE_EXECUTABLE"; exit 1; }
  exec "$DEDUPE_EXECUTABLE" ui
elif [[ -n "$REPO" && -x "$REPO/.venv/bin/dedupe" ]]; then
  cd "$REPO"
  exec "$REPO/.venv/bin/dedupe" ui
elif command -v dedupe >/dev/null 2>&1; then
  exec "$(command -v dedupe)" ui
elif [[ -n "$REPO" && -d "$REPO/src/dedupe" ]]; then
  PYTHON=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      PYTHON="$(command -v "$candidate")"; break
    fi
  done
  [[ -n "$PYTHON" ]] || { alert "Python 3.11+ was not found. Install Dedupe first."; exit 1; }
  cd "$REPO"
  export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
  exec "$PYTHON" -m dedupe.cli ui
else
  alert "The Dedupe installation moved or is unavailable. Rebuild this app from the checkout."
  exit 1
fi
EOF
chmod 755 "$MACOS/Dedupe"

if [[ -z "$ICON" ]]; then
  for candidate in "$REPO/packaging/Dedupe.icns" "$REPO/assets/Dedupe.icns" "$REPO/assets/icon.png" "$REPO/src/dedupe/assets/icon.png"; do
    [[ -f "$candidate" ]] && { ICON="$candidate"; break; }
  done
fi
if [[ -n "$ICON" ]]; then
  [[ -f "$ICON" ]] || die "icon source not found: $ICON"
  case "${ICON##*.}" in
    icns|ICNS) cp "$ICON" "$RESOURCES/Dedupe.icns" ;;
    png|PNG)
      command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1 \
        || die 'PNG conversion requires sips and iconutil'
      ICONSET="$(mktemp -d "${TMPDIR:-/tmp}/dedupe-icon.XXXXXX")"
      trap 'rm -rf "$ICONSET"' EXIT
      for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$ICON" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        sips -z "$double" "$double" "$ICON" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
      done
      iconutil -c icns "$ICONSET" -o "$RESOURCES/Dedupe.icns"
      rm -rf "$ICONSET"; trap - EXIT ;;
    *) die 'icon must be an .icns or PNG file' ;;
  esac
fi

/usr/bin/plutil -lint "$CONTENTS/Info.plist" >/dev/null
if ((ADHOC_SIGN)); then
  command -v codesign >/dev/null 2>&1 || die 'codesign is unavailable'
  codesign --force --sign - "$OUTPUT"
fi
printf 'Built %s%s\n' "$OUTPUT" "$([[ -z "$ICON" ]] && printf ' (no icon source found)' || true)"
