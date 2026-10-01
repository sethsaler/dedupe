# Local verification and CI

## One full local command

Run from the repository root on macOS or Linux. Use Python 3.11 or newer and
install the development dependencies, optional OpenCV detector, ffmpeg/ffprobe,
and Playwright Chromium first:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --editable '.[dev,human]'

# macOS
brew install ffmpeg
.venv/bin/python -m playwright install chromium

# Debian/Ubuntu alternative (requires sudo for OS packages)
sudo apt-get update && sudo apt-get install --yes ffmpeg
.venv/bin/python -m playwright install --with-deps chromium
```

Then run:

```bash
.venv/bin/python scripts/verify.py
```

This fails on a missing prerequisite or failed check. It runs Ruff, all
non-browser tests (including media integration when the required tools are
installed), Chromium end-to-end tests, source/wheel builds, and a fresh wheel
installation smoke test (`pip check`, `dedupe --help`, and `dedupe doctor --json`).
On macOS it also builds the unsigned app launcher. Temporary build outputs and
wheel environments are cleaned up; nothing is signed, notarized, uploaded, or
published. Python dependency installation and isolated builds require access to
the configured package index. Browser setup downloads Chromium once.

The full command covers the current OS and Python interpreter. Run it on macOS
to exercise macOS-only behavior; a Linux run cannot validate Finder, Trash, or
the macOS app bundle. The manual CI matrix covers supported Python versions
3.11–3.14 and the dedicated macOS Python 3.13 lane. Tests of the optional Photon
backend use fakes; private-media/model benchmarks remain opt-in and are described
in the main README. Full verification does not download Photon model weights.

Individual suites are available for iteration:

```bash
.venv/bin/python scripts/verify.py lint
.venv/bin/python scripts/verify.py unit
.venv/bin/python scripts/verify.py browser
.venv/bin/python scripts/verify.py package
.venv/bin/python scripts/verify.py macos  # macOS only, unsigned launcher
```

The `unit` suite respects the repository's non-browser boundary. It can skip
optional detector/media coverage when its dependencies are absent; install all
prerequisites and use the full command before relying on that coverage. The
repository currently has no separate type-check or format-check configuration;
the existing Ruff lint gate remains enabled.

## Automatic versus manual GitHub checks

- Pull requests and pushes to `main`: Python 3.11 non-browser tests and Ruff;
  ffmpeg is not installed in this lane. The two real-video integration tests
  skip if ffmpeg/ffprobe are unavailable, while mocked video tests and the
  file-safety/API-security tests still run. Optional OpenCV tests also require
  the separately installed `human` extra
- **CI → Run workflow**: all four Python versions, Chromium tests, the macOS
  tests and unsigned launcher build, Ruff, and Python distribution validation
- Every tag: the same complete CI validation matrix as before
- `v*` tags: the separate **Release artifacts** workflow still builds, verifies,
  uploads, and publishes the Python distributions using its existing tag gate

Manual CI runs consume GitHub Actions minutes; local verification does not.
Repository Actions must be enabled separately before any workflow can run.
These workflow changes do not change that setting or dispatch any jobs.

Existing check display names are retained, and expensive checks are gated at the
job level so ordinary PRs record skipped checks rather than waiting for a
workflow excluded by a path filter. The historical **Lint and package** check
always lints; its packaging step only runs manually or on tags. No run-level
concurrency cancellation is configured, so new runs do not cancel running or
queued work. Known docs-only changes skip the two automatic check jobs after a small path-classification
job. Source, configuration, unknown files, empty diffs, and unavailable diffs always
run checks. Tag and manual runs always run every lane. Renames include both old
and new paths in the classification.

Release artifact names, retention, wheel verification, release publishing, and
the explicit signing/notarization steps documented in `packaging/README.md`
are unchanged. Running manual CI never publishes a release. To validate the
expensive lanes before a release, run **CI → Run workflow** against the desired
commit and review the results before creating a release tag.
