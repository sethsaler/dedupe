#!/bin/bash
# Convenience double-click launcher at repo root.
# Delegates to launchers/Dedupe.command
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/launchers/Dedupe.command"
