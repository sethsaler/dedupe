# AGENTS

The product description — user-experience documents, verification checklists, and
bug triage for this tool — lives in `docs/product-description/`; start at its
`README.md`, then `goal.md`.

Verification commands for this repository (run from the repo root, using the
checked-in virtualenv at `.venv/`):

```bash
.venv/bin/pytest                 # non-browser suite
.venv/bin/ruff check src tests   # lint
```

Browser end-to-end tests are marked `e2e`. `pyproject.toml` sets
`addopts = "-m 'not e2e'"`, so a plain `pytest` run deselects them. Run them
explicitly (requires Playwright Chromium, installed with
`python -m playwright install chromium`):

```bash
.venv/bin/pytest -m e2e
```

Other `pyproject.toml` settings that matter:

- `[tool.pytest.ini_options]` sets `pythonpath = ["src"]`, so tests import
  `dedupe` from the working tree without installing it.
- `[tool.ruff]` sets `line-length = 100` and `target-version = "py311"`.

The complete local verification command is `.venv/bin/python scripts/verify.py`.
See `docs/verification.md` for ffmpeg, OpenCV, and Chromium prerequisites. It runs
lint, non-browser tests, browser tests, wheel validation, and the unsigned app
bundle on macOS. It does not sign, notarize, or publish a release.
