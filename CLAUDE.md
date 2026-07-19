# ledsign-studio

Tools for authoring and driving BMP-based LED signs: a browser editor, a pluggable
sign-driver layer, and documented wire protocols for controllers with no public spec.

## Stack / conventions
- **Python 3.10+**, standard library only in the core (`ledsign/bmp.py` has no deps).
  Pillow is optional and used solely by `bmp.from_image`.
- **Web editor is a single self-contained HTML file** — no build step, no framework, no CDN.
  It must keep working by double-clicking it.
- Sign backends implement `ledsign/drivers/base.py::SignDriver`. Nothing above the driver
  layer may know which sign family it is talking to.
- The JS BMP encoder in `web/index.html` and `ledsign/bmp.py` must stay byte-identical.
  There is a cross-check for this — run it if you touch either.

## Reality check
- **`private/` is gitignored and must stay that way.** It holds real customer sign content,
  packet captures, vendor binaries and raw RE notes. This repo is public. Before committing,
  confirm nothing from `private/` is staged and no customer identifiers leaked into docs.
- Writes to real signs are **opt-in** (`allow_writes=True`). Firmware, provisioning and
  network-reconfiguration opcodes are deliberately NOT implemented — do not add them.
- The DayStar playlist container is only partially decoded; playlists are produced by
  editing a captured template, not synthesised. Don't claim otherwise in docs.
- Protocol facts came from packet captures, verified by hash-matching a frame round-trip.
  Hold new claims to that same standard: if it wasn't observed on the wire or verified
  against ground truth, mark it PARTIAL.
