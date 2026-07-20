#!/usr/bin/env bash
# Install or update Dedupe from its public GitHub repository.
set -euo pipefail

REPO_URL="https://github.com/sethsaler/dedupe.git"
BRANCH="main"
INSTALL_DIR="${DEDUPE_INSTALL_DIR:-$HOME/.local/share/dedupe}"
BIN_DIR="${DEDUPE_BIN_DIR:-$HOME/.local/bin}"

die() {
  echo "Dedupe installer: $1" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || die "git is required. Install Git and try again."

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
[[ -n "$PYTHON" ]] || die "Python 3.11 or newer is required. Install it and try again."

if [[ -d "$INSTALL_DIR/.git" ]]; then
  origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  case "$origin" in
    "$REPO_URL"|https://github.com/sethsaler/dedupe|git@github.com:sethsaler/dedupe.git) ;;
    *) die "$INSTALL_DIR is a Git checkout with a different origin ($origin). Set DEDUPE_INSTALL_DIR to another path." ;;
  esac

  if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]]; then
    die "$INSTALL_DIR has local changes. Commit or remove them before updating."
  fi

  echo "Updating Dedupe in $INSTALL_DIR..."
  git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
  git -C "$INSTALL_DIR" merge --ff-only --quiet FETCH_HEAD \
    || die "the installed checkout cannot be fast-forwarded. Resolve it in $INSTALL_DIR and try again."
elif [[ -e "$INSTALL_DIR" ]]; then
  die "$INSTALL_DIR already exists and is not a Dedupe Git checkout. Set DEDUPE_INSTALL_DIR to another path."
else
  echo "Installing Dedupe in $INSTALL_DIR..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
fi

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]] \
  || ! "$INSTALL_DIR/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  rm -rf "$INSTALL_DIR/.venv"
  "$PYTHON" -m venv "$INSTALL_DIR/.venv" \
    || die "could not create a virtual environment. Ensure Python's venv module is installed."
fi

echo "Installing dependencies..."
"$INSTALL_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install --quiet --editable "${INSTALL_DIR}[human]"

mkdir -p "$BIN_DIR"
launcher="$BIN_DIR/dedupe"
if [[ -e "$launcher" && ! -L "$launcher" ]]; then
  die "$launcher already exists and is not a symlink. Remove it or set DEDUPE_BIN_DIR to another path."
fi
ln -sfn "$INSTALL_DIR/.venv/bin/dedupe" "$launcher"

echo ""
echo "Dedupe is ready."
echo "  Start the web UI: $launcher ui"
echo "  Scan from the CLI: $launcher scan ~/Pictures"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "Add $BIN_DIR to PATH to run 'dedupe' without its full path:"
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo ""
  echo "Note: ffmpeg and ffprobe are required for video support."
  echo "On macOS with Homebrew: brew install ffmpeg"
fi
