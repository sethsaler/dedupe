#!/usr/bin/env python3
"""Regenerate packaging/Dedupe.png (requires Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SCALE = 2
CANVAS = SIZE * SCALE
OUT = Path(__file__).with_name("Dedupe.png")


def lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def gradient() -> Image.Image:
    top_left = (64, 108, 255)
    bottom_right = (139, 73, 235)
    image = Image.new("RGB", (CANVAS, CANVAS))
    pixels = image.load()
    for y in range(CANVAS):
        for x in range(CANVAS):
            t = (x + y) / (2 * (CANVAS - 1))
            pixels[x, y] = tuple(lerp(top_left[i], bottom_right[i], t) for i in range(3))
    return image


def rounded_card(size: tuple[int, int], radius: int, fill: tuple[int, int, int, int]) -> Image.Image:
    card = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        radius=radius,
        fill=fill,
    )
    return card


def main() -> None:
    icon = gradient().convert("RGBA")

    # Quiet depth behind the cards.
    glow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        (CANVAS * 0.08, CANVAS * 0.02, CANVAS * 0.78, CANVAS * 0.72),
        fill=(255, 255, 255, 28),
    )
    glow_draw.ellipse(
        (CANVAS * 0.46, CANVAS * 0.42, CANVAS * 1.12, CANVAS * 1.08),
        fill=(0, 0, 80, 28),
    )
    icon.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90 * SCALE)))

    def paste_card(card: Image.Image, center: tuple[int, int], angle: float) -> None:
        shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle(
            (0, 0, card.size[0] - 1, card.size[1] - 1),
            radius=int(card.size[0] * 0.12),
            fill=(18, 20, 80, 90),
        )
        rotated_shadow = shadow.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        rotated = card.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        x = center[0] - rotated.size[0] // 2
        y = center[1] - rotated.size[1] // 2
        shadow_layer = Image.new("RGBA", icon.size, (0, 0, 0, 0))
        shadow_layer.alpha_composite(rotated_shadow, (x + 16 * SCALE, y + 22 * SCALE))
        icon.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(16 * SCALE)))
        icon.alpha_composite(rotated, (x, y))

    back = rounded_card((410 * SCALE, 500 * SCALE), 56 * SCALE, (218, 232, 255, 218))
    back_draw = ImageDraw.Draw(back)
    back_draw.rounded_rectangle(
        (38 * SCALE, 48 * SCALE, 372 * SCALE, 320 * SCALE),
        radius=32 * SCALE,
        fill=(103, 145, 255, 120),
    )
    paste_card(back, (535 * SCALE, 495 * SCALE), 8)

    front = rounded_card((430 * SCALE, 520 * SCALE), 60 * SCALE, (255, 255, 255, 246))
    draw = ImageDraw.Draw(front)
    draw.rounded_rectangle(
        (42 * SCALE, 52 * SCALE, 388 * SCALE, 330 * SCALE),
        radius=34 * SCALE,
        fill=(216, 229, 255, 255),
    )
    draw.ellipse(
        (286 * SCALE, 84 * SCALE, 348 * SCALE, 146 * SCALE),
        fill=(255, 198, 74, 255),
    )
    draw.polygon(
        [
            (54 * SCALE, 320 * SCALE),
            (164 * SCALE, 176 * SCALE),
            (250 * SCALE, 320 * SCALE),
        ],
        fill=(67, 116, 233, 255),
    )
    draw.polygon(
        [
            (174 * SCALE, 320 * SCALE),
            (270 * SCALE, 196 * SCALE),
            (378 * SCALE, 320 * SCALE),
        ],
        fill=(75, 172, 143, 255),
    )
    draw.rounded_rectangle(
        (42 * SCALE, 366 * SCALE, 250 * SCALE, 398 * SCALE),
        radius=16 * SCALE,
        fill=(93, 107, 145, 80),
    )
    draw.rounded_rectangle(
        (42 * SCALE, 420 * SCALE, 328 * SCALE, 446 * SCALE),
        radius=13 * SCALE,
        fill=(93, 107, 145, 54),
    )
    paste_card(front, (468 * SCALE, 505 * SCALE), -8)

    # Match badge: a compact check in the lower-right overlap.
    badge_center = (720 * SCALE, 735 * SCALE)
    badge_radius = 92 * SCALE
    badge_shadow = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(badge_shadow)
    shadow_draw.ellipse(
        (
            badge_center[0] - badge_radius,
            badge_center[1] - badge_radius + 16 * SCALE,
            badge_center[0] + badge_radius,
            badge_center[1] + badge_radius + 16 * SCALE,
        ),
        fill=(20, 20, 80, 80),
    )
    icon.alpha_composite(badge_shadow.filter(ImageFilter.GaussianBlur(18 * SCALE)))
    badge_draw = ImageDraw.Draw(icon)
    badge_draw.ellipse(
        (
            badge_center[0] - badge_radius,
            badge_center[1] - badge_radius,
            badge_center[0] + badge_radius,
            badge_center[1] + badge_radius,
        ),
        fill=(30, 205, 128, 255),
    )
    badge_draw.line(
        [
            (670 * SCALE, 738 * SCALE),
            (707 * SCALE, 775 * SCALE),
            (779 * SCALE, 685 * SCALE),
        ],
        fill=(255, 255, 255, 255),
        width=34 * SCALE,
        joint="curve",
    )

    icon = icon.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    icon.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
