"""
bmp.py — 24-bit uncompressed BMP encode/decode for LED sign frames.

LED sign controllers in this family consume plain Windows 3.x BMPs:
24 bits per pixel, BI_RGB (uncompressed), no palette. A frame is therefore

    54-byte header + width * height * 3 bytes of pixel data
    (+ row padding, when width*3 is not a multiple of 4)

For a 112x32 panel that is exactly 10806 bytes.

Two details that bite every first implementation:
  * BMP rows are stored **bottom-up** (last row of the image comes first).
  * Channels are **BGR**, not RGB.

This module has no third-party dependencies so it can run anywhere; PIL is used
only in the optional `from_image` helper.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass

HEADER_SIZE = 54
BITS_PER_PIXEL = 24


def row_stride(width: int) -> int:
    """Bytes per pixel row, padded up to a 4-byte boundary."""
    return ((width * 3 + 3) // 4) * 4


def expected_size(width: int, height: int) -> int:
    """Exact byte size of an encoded frame — useful for validating against a sign."""
    return HEADER_SIZE + row_stride(width) * height


@dataclass
class Frame:
    """A raw RGB frame. `pixels` is row-major, top-down, 3 bytes per pixel (RGB)."""
    width: int
    height: int
    pixels: bytearray

    @classmethod
    def blank(cls, width: int, height: int, color=(0, 0, 0)) -> "Frame":
        r, g, b = color
        return cls(width, height, bytearray(bytes((r, g, b)) * (width * height)))

    def get(self, x: int, y: int) -> tuple:
        i = (y * self.width + x) * 3
        return (self.pixels[i], self.pixels[i + 1], self.pixels[i + 2])

    def set(self, x: int, y: int, color) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        i = (y * self.width + x) * 3
        self.pixels[i], self.pixels[i + 1], self.pixels[i + 2] = color[0], color[1], color[2]

    def paste(self, other: "Frame", ox: int, oy: int) -> None:
        """Composite another frame at (ox, oy), clipped to bounds.

        This is what "drag an image onto the panel and move it around" reduces to.
        """
        for y in range(other.height):
            ty = oy + y
            if not (0 <= ty < self.height):
                continue
            for x in range(other.width):
                tx = ox + x
                if 0 <= tx < self.width:
                    self.set(tx, ty, other.get(x, y))


# ---------------------------------------------------------------- encode
def encode(frame: Frame) -> bytes:
    """Serialize a Frame to a 24-bit uncompressed BMP."""
    stride = row_stride(frame.width)
    pixel_bytes = stride * frame.height
    out = bytearray(HEADER_SIZE + pixel_bytes)

    # BITMAPFILEHEADER (14 bytes)
    out[0:2] = b"BM"
    struct.pack_into("<I", out, 2, HEADER_SIZE + pixel_bytes)   # file size
    struct.pack_into("<I", out, 10, HEADER_SIZE)                # pixel data offset

    # BITMAPINFOHEADER (40 bytes)
    struct.pack_into("<I", out, 14, 40)                 # header size
    struct.pack_into("<i", out, 18, frame.width)
    struct.pack_into("<i", out, 22, frame.height)       # positive => bottom-up
    struct.pack_into("<H", out, 26, 1)                  # planes
    struct.pack_into("<H", out, 28, BITS_PER_PIXEL)
    struct.pack_into("<I", out, 30, 0)                  # BI_RGB, uncompressed
    struct.pack_into("<I", out, 34, pixel_bytes)

    # pixel data: bottom-up rows, BGR order
    for y in range(frame.height):
        src = (frame.height - 1 - y) * frame.width * 3
        dst = HEADER_SIZE + y * stride
        for x in range(frame.width):
            r = frame.pixels[src + x * 3]
            g = frame.pixels[src + x * 3 + 1]
            b = frame.pixels[src + x * 3 + 2]
            out[dst + x * 3]     = b
            out[dst + x * 3 + 1] = g
            out[dst + x * 3 + 2] = r
    return bytes(out)


# ---------------------------------------------------------------- decode
def decode(data: bytes) -> Frame:
    """Parse a 24-bit uncompressed BMP into a Frame."""
    if data[:2] != b"BM":
        raise ValueError("not a BMP (missing 'BM' magic)")
    offset = struct.unpack_from("<I", data, 10)[0]
    width  = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bpp    = struct.unpack_from("<H", data, 28)[0]
    comp   = struct.unpack_from("<I", data, 30)[0]
    if bpp != 24:
        raise ValueError(f"expected 24bpp, got {bpp}")
    if comp != 0:
        raise ValueError(f"expected uncompressed BI_RGB, got compression {comp}")

    top_down = height < 0
    height = abs(height)
    stride = row_stride(width)
    px = bytearray(width * height * 3)
    for y in range(height):
        src_row = y if top_down else (height - 1 - y)
        src = offset + src_row * stride
        dst = y * width * 3
        for x in range(width):
            b = data[src + x * 3]
            g = data[src + x * 3 + 1]
            r = data[src + x * 3 + 2]
            px[dst + x * 3]     = r
            px[dst + x * 3 + 1] = g
            px[dst + x * 3 + 2] = b
    return Frame(width, height, px)


def validate(data: bytes, width: int, height: int) -> None:
    """Raise if `data` is not exactly the frame a sign of this geometry expects."""
    want = expected_size(width, height)
    if len(data) != want:
        raise ValueError(f"frame is {len(data)} bytes, sign expects {want} ({width}x{height})")
    f = decode(data)
    if (f.width, f.height) != (width, height):
        raise ValueError(f"frame is {f.width}x{f.height}, sign expects {width}x{height}")


# ---------------------------------------------------------------- PIL bridge
def from_image(path_or_img, width: int, height: int, fit: str = "contain",
               background=(0, 0, 0), offset=(0, 0)) -> Frame:
    """Load any image (PNG/JPG/GIF/...) and place it on a panel-sized frame.

    fit:
      "contain" — scale to fit inside the panel, preserving aspect
      "cover"   — scale to fill the panel, cropping the overflow
      "stretch" — distort to exactly the panel size
      "none"    — no scaling; place at native size (use with `offset` to pan)

    Requires Pillow. Everything else in this module is dependency-free.
    """
    from PIL import Image
    img = Image.open(path_or_img) if isinstance(path_or_img, (str, bytes)) else path_or_img
    img = img.convert("RGB")

    if fit == "stretch":
        img = img.resize((width, height), Image.NEAREST)
    elif fit in ("contain", "cover"):
        sx, sy = width / img.width, height / img.height
        s = min(sx, sy) if fit == "contain" else max(sx, sy)
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))),
                         Image.LANCZOS)
    # "none" -> leave as-is

    canvas = Frame.blank(width, height, background)
    layer = Frame(img.width, img.height, bytearray(img.tobytes()))
    # centre it, then apply the caller's nudge
    ox = (width - img.width) // 2 + offset[0]
    oy = (height - img.height) // 2 + offset[1]
    canvas.paste(layer, ox, oy)
    return canvas
