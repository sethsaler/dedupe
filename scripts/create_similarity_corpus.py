#!/usr/bin/env python3
"""Make a reproducible labeled smoke corpus; never use it as real-library proof."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory (must not exist)")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1]
                        / "tests/fixtures/astronaut.png")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with Image.open(args.source) as source:
        original = source.convert("RGB")
    original.save(args.output / "original.png")
    width, height = original.size
    variants = {
        "resize": original.resize((width // 2, height // 2)),
        "compression": original,
        "rotation": original.transpose(Image.Transpose.ROTATE_90),
        "crop": original.crop((width // 8, height // 8, width * 7 // 8, height * 7 // 8)),
        "brightness": ImageEnhance.Brightness(original).enhance(1.15),
        "grayscale": ImageOps.grayscale(original).convert("RGB"),
        "different": Image.new("RGB", original.size, (10, 160, 90)),
        "local-edit": original.copy(),
    }
    variants["local-edit"].paste((0, 0, 0), (width // 3, height // 4, width // 2, height // 2))
    pairs = []
    for name, image in variants.items():
        filename = name + (".jpg" if name == "compression" else ".png")
        image.save(args.output / filename, **({"quality": 65} if name == "compression" else {}))
        pairs.append({"path_a": "original.png", "path_b": filename,
                      "similar": name not in ("different", "local-edit"), "category": name})
    (args.output / "pairs.json").write_text(json.dumps({"pairs": pairs}, indent=2) + "\n")
    print(args.output / "pairs.json")


if __name__ == "__main__":
    main()
