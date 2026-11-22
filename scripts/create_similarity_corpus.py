#!/usr/bin/env python3
"""Make a reproducible labeled smoke corpus; never use it as real-library proof."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
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


def _make_screenshot(original: Image.Image) -> Image.Image:
    width, height = original.size
    canvas_h = max(height + max(48, height // 2), round(width * 19 / 9))
    screen = Image.new("RGB", (width, canvas_h), (8, 8, 10))
    draw = ImageDraw.Draw(screen)
    bar_h = max(12, canvas_h // 24)
    # Minimal phone status bar and bottom home indicator on black letterboxing.
    draw.rectangle((width // 12, bar_h // 3, width // 5, bar_h * 2 // 3), fill=(220, 220, 220))
    draw.rectangle((width * 4 // 5, bar_h // 3, width * 11 // 12, bar_h * 2 // 3), fill=(220, 220, 220))
    indicator_y = canvas_h - max(8, canvas_h // 40)
    draw.rectangle((width // 3, indicator_y - 2, width * 2 // 3, indicator_y + 2), fill=(230, 230, 230))
    top_y = (canvas_h - height) // 2
    screen.paste(original, (0, top_y))
    return screen


def _apply_scurve(original: Image.Image) -> Image.Image:
    x = np.linspace(0.0, 1.0, 256)
    lut = np.clip(255.0 * (3.0 * x * x - 2.0 * x * x * x), 0, 255).astype("uint8")
    return Image.fromarray(lut[np.asarray(original)])


def _isoluminant_patch(original: Image.Image) -> Image.Image:
    width, height = original.size
    arr = np.asarray(original, dtype=np.int16).copy()
    patch = arr[height // 4 : height * 3 // 4, width // 4 : width * 3 // 4]
    # Shift R and G in equal-luma proportion (0.299 * 58 - 0.587 * 30 ~ 0).
    patch[:, :, 0] = np.clip(patch[:, :, 0] + 58, 0, 255)
    patch[:, :, 1] = np.clip(patch[:, :, 1] - 30, 0, 255)
    return Image.fromarray(arr.astype("uint8"))


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
    mirrored = ImageOps.mirror(original)
    variants = {
        "resize": original.resize((width // 2, height // 2)),
        "compression": original,
        "rotation": original.transpose(Image.Transpose.ROTATE_90),
        "crop": original.crop((width // 8, height // 8, width * 7 // 8, height * 7 // 8)),
        "crop-4x3": original.crop((0, height // 8, width, height * 7 // 8)),
        "crop-tight": original.crop((width // 6, height // 6, width * 5 // 6, height * 5 // 6)),
        "screenshot": _make_screenshot(original),
        "scurve": _apply_scurve(original),
        "mirror": mirrored,
        "mirror-crop": mirrored.crop((width // 10, height // 10, width * 9 // 10, height * 9 // 10)),
        "mirror-bw": ImageOps.grayscale(mirrored).convert("RGB"),
        "brightness": ImageEnhance.Brightness(original).enhance(1.15),
        "grayscale": ImageOps.grayscale(original).convert("RGB"),
        "different": Image.new("RGB", original.size, (10, 160, 90)),
        "local-edit": original.copy(),
        "isoluminant-patch": _isoluminant_patch(original),
    }
    variants["local-edit"].paste((0, 0, 0), (width // 3, height // 4, width // 2, height // 2))
    pairs = []
    for name, image in variants.items():
        filename = name + (".jpg" if name == "compression" else ".png")
        image.save(args.output / filename, **({"quality": 65} if name == "compression" else {}))
        category = "isoluminant-negative" if name == "isoluminant-patch" else name
        pairs.append({
            "path_a": "original.png",
            "path_b": filename,
            "similar": name not in ("different", "local-edit", "isoluminant-patch"),
            "category": category,
        })

    # Isoluminant card negative pair (same grayscale structure & luma, opposing hues).
    iso_a = Image.new("RGB", (256, 256), (200, 40, 40))
    iso_b = Image.new("RGB", (256, 256), (40, 121, 40))
    for card in (iso_a, iso_b):
        draw = ImageDraw.Draw(card)
        draw.ellipse((20, 30, 150, 170), fill="white")
        draw.rectangle((160, 80, 210, 240), fill="black")
    iso_a.save(args.output / "isoluminant-red.png")
    iso_b.save(args.output / "isoluminant-green.png")
    pairs.append({
        "path_a": "isoluminant-red.png",
        "path_b": "isoluminant-green.png",
        "similar": False,
        "category": "isoluminant-negative",
    })

    # Three-file edited-copy set (original + recompression + crop).
    set_files = ["set-01-orig.png", "set-01-recompress.jpg", "set-01-crop.jpg"]
    original.save(args.output / set_files[0])
    original.save(args.output / set_files[1], quality=72)
    original.crop((width // 10, height // 10, width * 9 // 10, height * 9 // 10)).save(
        args.output / set_files[2], quality=85
    )
    for i, path_a in enumerate(set_files):
        for path_b in set_files[i + 1 :]:
            pairs.append({
                "path_a": path_a,
                "path_b": path_b,
                "similar": True,
                "category": "three-file-set",
            })

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
    manifest = {
        "pairs": pairs,
        "sets": [{"files": set_files, "category": "three-file-set"}],
    }
    (args.output / "pairs.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(args.output / "pairs.json")


if __name__ == "__main__":
    main()
