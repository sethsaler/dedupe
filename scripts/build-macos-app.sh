#!/usr/bin/env bash
# Build the self-contained macOS Dedupe.app bundle.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build-macos-app.sh [options]

Build a self-contained Dedupe.app (default: build/Dedupe.app). The bundle
contains a native AppKit/WKWebView shell and an embedded Python backend; it
does not require the repository checkout or a pre-existing dedupe install.

Options:
  --output PATH       Bundle output path
  --python PATH       Python 3.11+ used for the build environment (default: python3)
  --no-human         Omit the optional OpenCV person/face detectors
  --no-smoke         Skip the embedded-backend launch smoke test
  --no-adhoc-sign    Do not apply the default local ad-hoc signature
  --zip PATH         Also write a zip suitable for sharing the app
  --keep-work        Keep build/macos-app-work for debugging
  -h, --help          Show this help

ffmpeg and ffprobe are not embedded. At runtime the app looks in Homebrew's
standard locations and ~/.local/bin before the system PATH.
EOF
}

die() { printf 'build-macos-app: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$REPO/build/Dedupe.app"
PYTHON_BIN="${PYTHON:-}"
PYTHON_EXPLICIT=0
if [[ -n "$PYTHON_BIN" ]]; then
  PYTHON_EXPLICIT=1
fi
INCLUDE_HUMAN=1
SMOKE=1
ADHOC_SIGN=1
ZIP_PATH=""
KEEP_WORK=0

while (($#)); do
  case "$1" in
    --output) (($# >= 2)) || die '--output requires a path'; OUTPUT="$2"; shift 2 ;;
    --python) (($# >= 2)) || die '--python requires a path'; PYTHON_BIN="$2"; PYTHON_EXPLICIT=1; shift 2 ;;
    --no-human) INCLUDE_HUMAN=0; shift ;;
    --no-smoke) SMOKE=0; shift ;;
    --no-adhoc-sign) ADHOC_SIGN=0; shift ;;
    --zip) (($# >= 2)) || die '--zip requires a path'; ZIP_PATH="$2"; shift 2 ;;
    --keep-work) KEEP_WORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ "$(uname -s)" == Darwin ]] || die 'macOS is required'
[[ -d "$REPO/src/dedupe" ]] || die "not a Dedupe checkout: $REPO"
[[ "$OUTPUT" == *.app ]] || die '--output must end in .app'
command -v swiftc >/dev/null 2>&1 \
  || die 'swiftc is unavailable; install Xcode or the Xcode command-line tools'
command -v sips >/dev/null 2>&1 || die 'sips is unavailable'
command -v iconutil >/dev/null 2>&1 || die 'iconutil is unavailable'
command -v plutil >/dev/null 2>&1 || die 'plutil is unavailable'
python_is_usable() {
  [[ -x "$1" ]] && "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null
}
python_is_stable() {
  [[ -x "$1" ]] && "$1" -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info < (3, 15)))' 2>/dev/null
}
if ((PYTHON_EXPLICIT)); then
  if [[ "$PYTHON_BIN" != */* ]]; then
    PYTHON_BIN="$(command -v "$PYTHON_BIN" 2>/dev/null || true)"
  fi
else
  PYTHON_BIN=""
  if [[ -x "$REPO/.venv/bin/python" ]] && python_is_stable "$REPO/.venv/bin/python"; then
    PYTHON_BIN="$REPO/.venv/bin/python"
  fi
  if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
      resolved="$(command -v "$candidate" 2>/dev/null || true)"
      if [[ -n "$resolved" ]] && python_is_stable "$resolved"; then
        PYTHON_BIN="$resolved"
        break
      fi
    done
  fi
  if [[ -z "$PYTHON_BIN" ]]; then
    resolved="$(command -v python3 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && python_is_usable "$resolved"; then
      printf 'warning: no Python 3.11–3.14 found; falling back to %s\n' "$resolved" >&2
      PYTHON_BIN="$resolved"
    fi
  fi
fi
python_is_usable "$PYTHON_BIN" \
  || die 'Python 3.11+ is required; pass one with --python PATH'

WORK="$REPO/build/macos-app-work"
rm -rf "$WORK"
mkdir -p "$WORK"
cleanup() {
  if ((!KEEP_WORK)); then rm -rf "$WORK"; fi
}
trap cleanup EXIT

printf 'Creating build environment…\n'
"$PYTHON_BIN" -m venv "$WORK/venv"
PY="$WORK/venv/bin/python"
"$PY" -m pip install --upgrade pip >/dev/null
if ((INCLUDE_HUMAN)); then
  (cd "$REPO" && "$PY" -m pip install '.[human]' 'pyinstaller>=6.10')
else
  (cd "$REPO" && "$PY" -m pip install '.' 'pyinstaller>=6.10')
fi

VERSION="$("$PY" -c 'import importlib.metadata; print(importlib.metadata.version("dedupe-media"))')"
printf 'Embedding Dedupe %s backend with PyInstaller…\n' "$VERSION"
PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --onedir
  --console
  --name dedupe-server
  --distpath "$WORK/dist"
  --workpath "$WORK/pyinstaller"
  --specpath "$WORK/spec"
  --copy-metadata dedupe-media
  --collect-submodules dedupe
  --collect-data dedupe
  --collect-all pillow_heif
  --hiddenimport send2trash.mac.legacy
)
# OpenCV is discovered through dedupe's imports and PyInstaller's cv2 hook;
# --collect-all cv2 would duplicate its large shared libraries.
"$PY" -m PyInstaller "${PYINSTALLER_ARGS[@]}" "$REPO/scripts/macos_backend_entry.py"

BACKEND_DIR="$WORK/dist/dedupe-server"
BACKEND="$BACKEND_DIR/dedupe-server"
[[ -x "$BACKEND" ]] || die "PyInstaller did not produce $BACKEND"

if ((SMOKE)); then
  printf 'Smoke-testing the embedded backend…\n'
  mkdir -p "$WORK/smoke-home"
  BACKEND_PATH="$BACKEND" SMOKE_HOME="$WORK/smoke-home" "$PY" - <<'PY'
import os
import select
import subprocess
import sys
import time
import urllib.request

backend = os.environ["BACKEND_PATH"]
token = "build-smoke"
env = dict(os.environ)
# Launch with an empty library. Startup restores saved folder monitors and
# could otherwise scan the builder's real folders during bundle verification.
env["HOME"] = os.environ["SMOKE_HOME"]
env["XDG_STATE_HOME"] = os.path.join(env["HOME"], "state")
env["XDG_CACHE_HOME"] = os.path.join(env["HOME"], ".cache")
env["DEDUPE_NATIVE_SHELL"] = "1"
env["DEDUPE_NATIVE_TOKEN"] = token
process = subprocess.Popen(
    [backend, "ui", "--port", "0", "--no-browser"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
    env=env,
)
output: list[str] = []
try:
    url = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        ready, _, _ = select.select([process.stdout], [], [], 0.25)
        if not ready:
            continue
        line = process.stdout.readline() if process.stdout is not None else ""
        if not line:
            break
        output.append(line)
        if line.startswith("Dedupe UI: "):
            url = line.split("Dedupe UI: ", 1)[1].strip()
            break
    if not url:
        raise RuntimeError("embedded backend did not report a UI URL")

    with urllib.request.urlopen(url + "api/status", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"/api/status returned HTTP {response.status}")
    request = urllib.request.Request(
        url + "api/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-Dedupe-Native-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"/api/shutdown returned HTTP {response.status}")
    process.wait(timeout=8)
except Exception as exc:
    if process.poll() is None:
        process.kill()
    try:
        remainder = process.communicate(timeout=2)[0] or ""
    except Exception:
        remainder = ""
    output.append(remainder)
    print("embedded backend smoke test failed:", exc, file=sys.stderr)
    print("".join(output)[-12000:], file=sys.stderr)
    sys.exit(1)
finally:
    if process.poll() is None:
        process.kill()
        process.wait()
PY
fi

printf 'Assembling native app bundle…\n'
if [[ -e "$OUTPUT" ]]; then
  [[ -d "$OUTPUT/Contents" ]] || die "refusing to replace non-bundle path: $OUTPUT"
  rm -rf "$OUTPUT"
fi
mkdir -p "$OUTPUT/Contents/MacOS" "$OUTPUT/Contents/Resources"
cp -R "$BACKEND_DIR" "$OUTPUT/Contents/Resources/backend"
rm -f "$OUTPUT/Contents/Resources/backend/dedupe-server.spec"

BUILD_NUMBER="$(date +%Y%m%d%H%M%S)"
cat > "$OUTPUT/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Dedupe</string>
  <key>CFBundleDisplayName</key><string>Dedupe</string>
  <key>CFBundleIdentifier</key><string>com.sethsaler.dedupe</string>
  <key>CFBundleVersion</key><string>$BUILD_NUMBER</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Dedupe</string>
  <key>CFBundleIconFile</key><string>Dedupe.icns</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppSleepDisabled</key><true/>
</dict></plist>
EOF
plutil -lint "$OUTPUT/Contents/Info.plist" >/dev/null

export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"
swiftc -O \
  "$REPO/packaging/macos/main.swift" \
  -framework Cocoa -framework WebKit \
  -o "$OUTPUT/Contents/MacOS/Dedupe"
chmod 755 "$OUTPUT/Contents/MacOS/Dedupe"

ICON="$REPO/packaging/Dedupe.png"
if [[ -f "$ICON" ]]; then
  ICONSET="$WORK/Dedupe.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ICON" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$ICON" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$OUTPUT/Contents/Resources/Dedupe.icns"
else
  printf 'warning: packaging/Dedupe.png not found; using the generic app icon\n' >&2
fi

if ((ADHOC_SIGN)); then
  command -v codesign >/dev/null 2>&1 || die 'codesign is unavailable'
  command -v file >/dev/null 2>&1 || die 'file is unavailable'
  # Sign nested Mach-O binaries under Resources/backend inside-out rather than
  # relying on deprecated `codesign --deep`.
  while IFS= read -r -d '' candidate; do
    if file -b "$candidate" | grep -q 'Mach-O'; then
      codesign --force --sign - "$candidate"
    fi
  done < <(find "$OUTPUT/Contents/Resources/backend" -type f -print0)
  codesign --force --sign - "$OUTPUT"
fi

if [[ -n "$ZIP_PATH" ]]; then
  mkdir -p "$(dirname "$ZIP_PATH")"
  rm -f "$ZIP_PATH"
  /usr/bin/ditto -c -k --keepParent "$OUTPUT" "$ZIP_PATH"
fi

SIZE="$(du -sh "$OUTPUT" 2>/dev/null | awk '{print $1}')"
printf 'Built %s (%s)\n' "$OUTPUT" "${SIZE:-unknown size}"
[[ -z "$ZIP_PATH" ]] || printf 'Wrote %s\n' "$ZIP_PATH"
printf 'Open it with: open %q\n' "$OUTPUT"
