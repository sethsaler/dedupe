#!/usr/bin/env python3
"""Make a reproducible labeled smoke corpus; never use it as real-library proof."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageOps


def detail_variants(original: Image.Image) -> dict[str, Image.Image]:
    """Synthetic local edits, positioned on the default astronaut's features.

    Coordinates scale with custom sources; those edits remain useful local-change
    controls but do not necessarily land on a face in a different photograph.
    """
    width, height = original.size

    def point(x, y):
        return round(x * width / 512), round(y * height / 512)

    smile = original.copy()
    ImageDraw.Draw(smile).line(
        [point(x, y) for x, y in [(204, 145), (213, 147), (225, 147), (239, 144)]],
        fill=(110, 66, 56), width=max(1, round(3 * min(width, height) / 512)),
    )
    variants = {"smile": smile}
    for name, box, offset in [
        ("hair", (180, 37, 200, 63), (2, 0)),
        ("eyes", (183, 91, 266, 115), (2, 0)),
        ("pose", (178, 79, 270, 171), (1, 1)),
    ]:
        left, top = point(*box[:2])
        right, bottom = point(*box[2:])
        dx = max(1, round(offset[0] * width / 512)) if offset[0] else 0
        dy = max(1, round(offset[1] * height / 512)) if offset[1] else 0
        changed = original.copy()
        changed.paste(original.crop((left, top, right, bottom)), (left + dx, top + dy))
        variants[name] = changed
    return variants


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

    # Each changed image is distinct from the original but must still match its
    # own reexports. Exposure differences must not hide the local edit.
    original.save(args.output / "original-export.jpg", quality=92)
    for name, changed in detail_variants(original).items():
        source = f"detail-{name}-source.png"
        changed.save(args.output / source)
        for exposure, suffix in [(1.0, "compressed"), (1.08, "brighter")]:
            negative = f"detail-{name}-{suffix}.jpg"
            ImageEnhance.Brightness(changed).enhance(exposure).save(
                args.output / negative, quality=75
            )
            for reference, similar, category in [
                ("original-export.jpg", False, f"detail-{name}"),
                (source, True, "detail-reexport-control"),
            ]:
                pairs.append({"path_a": reference, "path_b": negative,
                              "similar": similar, "category": category})
    (args.output / "pairs.json").write_text(json.dumps({"pairs": pairs}, indent=2) + "\n")
    print(args.output / "pairs.json")


if __name__ == "__main__":
    main()
