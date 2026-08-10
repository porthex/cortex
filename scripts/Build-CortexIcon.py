"""Build the Cortex Windows tray icon with explicit small-size entries."""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "cortex-icon-v2.png"
OUTPUT_PNG = ROOT / "assets" / "cortex-icon.png"
OUTPUT_ICO = ROOT / "assets" / "cortex.ico"
VERSIONED_ICO = ROOT / "assets" / "cortex-v2.ico"
PREVIEW_DIR = ROOT / "runtime"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def cleaned_source() -> Image.Image:
    image = Image.open(SOURCE).convert("RGBA")
    pixels = image.load()
    # Chroma removal leaves magenta RGB in semitransparent edge pixels. The
    # circle's edge is navy, so neutralize that RGB while preserving its alpha.
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                pixels[x, y] = (0, 0, 0, 0)
            elif a < 255:
                pixels[x, y] = (7, 20, 69, a)

    bbox = image.getbbox()
    if bbox is None:
        raise RuntimeError(f"Icon source is empty: {SOURCE}")
    crop = image.crop(bbox)
    side = max(crop.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return square


def render_size(source: Image.Image, size: int) -> Image.Image:
    # Keep the badge nearly full-canvas so the glyph survives the 16px tray.
    inner = max(1, round(size * 0.96))
    scaled = source.resize((inner, inner), Image.Resampling.LANCZOS)
    if size <= 32:
        scaled = ImageEnhance.Contrast(scaled).enhance(1.08)
        scaled = scaled.filter(ImageFilter.UnsharpMask(radius=0.55, percent=135, threshold=2))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, ((size - inner) // 2, (size - inner) // 2))
    return canvas


def encode_png(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, "PNG", optimize=True)
    return stream.getvalue()


def write_ico(images: dict[int, Image.Image], path: Path) -> None:
    payloads = [(size, encode_png(images[size])) for size in SIZES]
    header_size = 6 + 16 * len(payloads)
    offset = header_size
    entries = []
    for size, payload in payloads:
        dimension = 0 if size == 256 else size
        entries.append(
            struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset)
        )
        offset += len(payload)
    with path.open("wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(payloads)))
        handle.write(b"".join(entries))
        handle.write(b"".join(payload for _, payload in payloads))


def main() -> None:
    source = cleaned_source()
    images = {size: render_size(source, size) for size in SIZES}
    OUTPUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    source.resize((1024, 1024), Image.Resampling.LANCZOS).save(OUTPUT_PNG, "PNG", optimize=True)
    write_ico(images, VERSIONED_ICO)
    write_ico(images, OUTPUT_ICO)
    for size in (16, 20, 24, 32):
        images[size].resize((256, 256), Image.Resampling.NEAREST).save(
            PREVIEW_DIR / f"cortex-icon-{size}x-preview.png", "PNG"
        )
    print(f"Built {OUTPUT_ICO} with sizes: {', '.join(map(str, SIZES))}")


if __name__ == "__main__":
    main()
