#!/usr/bin/env python3
"""Skip automatic checks only for a verified, non-empty docs-only diff."""

from __future__ import annotations

import os
import re
import subprocess

DOCUMENTATION = {
    "README.md",
    "AGENTS.md",
    "PLAN.md",
    "next_features.md",
    "packaging/README.md",
    "docs/verification.md",
}


def checks_required(paths: list[str]) -> bool:
    return not paths or any(
        path not in DOCUMENTATION
        and not (path.startswith("docs/product-description/") and path.endswith(".md"))
        for path in paths
    )


def main() -> None:
    required = True
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    force_full = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.environ.get(
        "GITHUB_REF", ""
    ).startswith("refs/tags/")
    valid_shas = all(
        re.fullmatch(r"[0-9a-fA-F]{40}", sha) and set(sha) != {"0"} for sha in (base, head)
    )
    if not force_full and valid_shas:
        try:
            # Renames must show both paths: moving source into docs still runs CI.
            diff = subprocess.run(
                ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
                check=True,
                capture_output=True,
            )
            required = checks_required(
                [os.fsdecode(path) for path in diff.stdout.split(b"\0") if path]
            )
        except (OSError, subprocess.CalledProcessError):
            pass  # An unavailable diff must never suppress checks.
    output = f"required={str(required).lower()}\n"
    print(output, end="")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
        file.write(output)


if __name__ == "__main__":
    main()
