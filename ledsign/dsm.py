#!/usr/bin/env python3
"""
dsm.py — importer for the ".dsm" playlist authoring format.

This is the authoring format used by the vendor Windows application. Signs never
see a .dsm — they are sent rendered bitmaps plus a playlist container — so this
module exists purely to IMPORT existing playlists into the editor.

A .dsm is a UTF-16LE text file, CRLF line endings, one record per line:

    KEY<space>value            (KEY is always 5 chars; value may contain TABs)

The file starts with a sign-config header, then a playlist of "frames".
Each frame begins with an FNAME record and runs until the next FNAME.

Header keys:
    VERSI  file format version (4)
    SIGNT  sign model            e.g. "Example LED Family"
    DISPN  display number        e.g. 100001
    DISPX  width  in pixels       e.g. 112
    DISPY  height in pixels       e.g. 32
    DISPS  number of sides        e.g. 2  (Side A / Side B)
    COLOR  "color" | "mono"
    WROTE  OLE-automation date serial

Per-frame keys (subset):
    FNAME  frame id
    DURRA  dwell time (seconds)
    SPEED  scroll/animation speed
    BACKG  background color, TAB-separated  R<TAB>G<TAB>B
    BACKI  pre-rendered bitmap filename  "<project>-<FNAME>.bmp"
    TEXTA  a text object (see TEXTA_FIELDS below)

TEXTA fields (TAB-separated after the "TEXTA " key):
    0  x (px)            8..11  outline/2nd color RGBA
    1  y (px)           12    bold   flag  (upper = on)
    2  font size (px)   13    italic flag
    3  font name        14    underline flag
    4..7 color RGBA     15    strike/shadow flag
                        16    align  L | C | R
    35 the text string
"""
from __future__ import annotations
import sys, os
from dataclasses import dataclass, field

# ---------------------------------------------------------------- data model
@dataclass
class Text:
    x: int; y: int; size: int; font: str
    color: tuple                       # (r,g,b,a)
    bold: bool; italic: bool; underline: bool
    align: str; text: str
    raw: list                          # original 36 fields, for lossless round-trip

@dataclass
class Frame:
    fname: str = ""
    duration: float = 0.0
    speed: int = 0
    bg: tuple = (0, 0, 0)
    backi: str = ""
    texts: list = field(default_factory=list)
    records: list = field(default_factory=list)   # (key, value) in original order

@dataclass
class Project:
    header: dict = field(default_factory=dict)     # key -> raw value string
    header_order: list = field(default_factory=list)
    frames: list = field(default_factory=list)

    # convenience
    @property
    def width(self):  return int(self.header.get("DISPX", "0") or 0)
    @property
    def height(self): return int(self.header.get("DISPY", "0") or 0)
    @property
    def sides(self):  return int(self.header.get("DISPS", "1") or 1)
    @property
    def model(self):  return self.header.get("SIGNT", "")
    @property
    def dispn(self):  return self.header.get("DISPN", "")

# ---------------------------------------------------------------- parsing
def _split_kv(line: str):
    """KEY is the first 5 chars; a single space separates it from the value."""
    if len(line) < 5:
        return line, ""
    key = line[:5]
    val = line[6:] if len(line) > 6 else ""
    return key, val

def _parse_text(val: str) -> Text:
    f = val.split("\t")
    while len(f) < 36:
        f.append("")
    def i(n, d=0):
        try: return int(f[n])
        except: return d
    return Text(
        x=i(0), y=i(1), size=i(2) or 8, font=f[3] or "Arial",
        color=(i(4, 255), i(5, 255), i(6, 255), i(7, 255)),
        bold=f[12].isupper() and f[12] != "",
        italic=f[13].isupper() and f[13] != "",
        underline=f[14].isupper() and f[14] != "",
        align=(f[16] or "L").upper()[:1],
        text=f[35],
        raw=f,
    )

def parse(path: str) -> Project:
    raw = open(path, "rb").read()
    # tolerate BOM / either encoding
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        try: text = raw.decode("utf-16-le")
        except UnicodeDecodeError: text = raw.decode("utf-8", "replace")
    lines = text.splitlines()

    proj = Project()
    cur: Frame | None = None
    for line in lines:
        if not line.strip():
            continue
        key, val = _split_kv(line)
        if key == "FNAME":
            cur = Frame(fname=val)
            proj.frames.append(cur)
        if cur is None:
            # header
            proj.header[key] = val
            proj.header_order.append(key)
            continue
        cur.records.append((key, val))
        if key == "DURRA":
            try: cur.duration = float(val)
            except: pass
        elif key == "SPEED":
            try: cur.speed = int(val)
            except: pass
        elif key == "BACKG":
            p = val.split("\t")
            if len(p) >= 3:
                try: cur.bg = (int(p[0]), int(p[1]), int(p[2]))
                except: pass
        elif key == "BACKI":
            cur.backi = val
        elif key == "TEXTA":
            cur.texts.append(_parse_text(val))
    return proj

# ---------------------------------------------------------------- rendering
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
]

def _load_font(size, bold):
    from PIL import ImageFont
    paths = _FONT_CANDIDATES
    if bold:
        paths = [p for p in paths if "Bold" in p] + paths
    for p in paths:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, max(6, size))
            except: pass
    return ImageFont.load_default()

def render_frame(frame: Frame, w: int, h: int):
    """Return a PIL.Image (w x h, RGB) approximating one frame on the sign."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), frame.bg)
    d = ImageDraw.Draw(img)
    for t in frame.texts:
        if not t.text:
            continue
        # The sign uses a condensed LED font; Arial is wider, so shrink to fit
        # the available width (from x to the right edge) to avoid false clipping.
        size = t.size
        font = _load_font(size, t.bold)
        avail = max(4, w - t.x) if t.align == "L" else w
        while size > 6 and d.textlength(t.text, font=font) > avail:
            size -= 1
            font = _load_font(size, t.bold)
        tw = d.textlength(t.text, font=font)
        x = t.x
        if t.align == "C": x = int(t.x - tw / 2)
        elif t.align == "R": x = int(t.x - tw)
        d.text((x, t.y), t.text, fill=t.color[:3], font=font)
        if t.underline:
            yb = t.y + t.size
            d.line((x, yb, x + tw, yb), fill=t.color[:3])
    return img

def render_contact_sheet(proj: Project, path: str, scale: int = 6, gap: int = 6):
    """Stack every frame vertically, scaled up, with labels — one PNG preview."""
    from PIL import Image, ImageDraw
    w, h = proj.width or 112, proj.height or 32
    label_h = 14
    cell_w = w * scale
    cell_h = h * scale + label_h
    sheet = Image.new("RGB", (cell_w + 2 * gap, (cell_h + gap) * len(proj.frames) + gap),
                      (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    small = _load_font(11, False)
    y = gap
    for fr in proj.frames:
        frame_img = render_frame(fr, w, h).resize((w * scale, h * scale), Image.NEAREST)
        sheet.paste(frame_img, (gap, y + label_h))
        txt = " | ".join(t.text.strip() for t in fr.texts if t.text.strip())
        d.text((gap + 2, y), f"frame {fr.fname}  {fr.duration:g}s  bg{fr.bg}  {txt}"[:120],
               fill=(180, 180, 190), font=small)
        y += cell_h + gap
    sheet.save(path)
    return path

def export_bmps(proj: Project, outdir: str):
    """Write each frame as a native width x height BMP (what the sign consumes)."""
    os.makedirs(outdir, exist_ok=True)
    w, h = proj.width or 112, proj.height or 32
    out = []
    for fr in proj.frames:
        img = render_frame(fr, w, h)
        name = os.path.join(outdir, f"frame-{fr.fname}.bmp")
        img.save(name)
        out.append(name)
    return out

# ---------------------------------------------------------------- writing
def dumps(proj: Project) -> str:
    lines = []
    for k in proj.header_order:
        lines.append(f"{k} {proj.header[k]}")
    for fr in proj.frames:
        for (k, v) in fr.records:
            lines.append(f"{k} {v}")
    return "\r\n".join(lines) + "\r\n"

def save(proj: Project, path: str):
    open(path, "wb").write(("﻿" + dumps(proj)).encode("utf-16-le"))

# ---------------------------------------------------------------- CLI
def _info(proj: Project):
    print(f"model   : {proj.model}")
    print(f"display : {proj.dispn}")
    print(f"size    : {proj.width} x {proj.height} px, {proj.sides} side(s)")
    print(f"frames  : {len(proj.frames)}")
    for fr in proj.frames:
        txt = " | ".join(t.text.strip() for t in fr.texts if t.text.strip())
        print(f"  [{fr.fname:>3}] {fr.duration:g}s  spd{fr.speed}  bg{fr.bg}  {txt}")

def main(argv):
    if len(argv) < 2:
        print("usage: dsm.py info|preview|bmps <file.dsm> [out]")
        return 1
    cmd, path = argv[0], argv[1]
    proj = parse(path)
    if cmd == "info":
        _info(proj)
    elif cmd == "preview":
        out = argv[2] if len(argv) > 2 else "preview.png"
        render_contact_sheet(proj, out)
        print("wrote", out)
    elif cmd == "bmps":
        out = argv[2] if len(argv) > 2 else "frames"
        files = export_bmps(proj, out)
        print(f"wrote {len(files)} bmp(s) to {out}/")
    else:
        print("unknown command:", cmd); return 1
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
