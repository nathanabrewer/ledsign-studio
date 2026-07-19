# LED Sign Studio

Tools for authoring and driving **BMP-based LED signs** — an editor, a pluggable driver
layer, and documented wire protocols for controllers that ship no public spec.

Most small commercial LED signs come with a single Windows application, no documentation,
and no way to script anything. Underneath, though, the job is simple: **build a 24-bit RGB
bitmap at the panel's resolution and get it onto the controller.** This project does that
part in the open.

## What's here

| | |
|---|---|
| `web/index.html` | Browser editor — drag any image onto the panel, move it live, pixel-accurate preview, per-side A/B, `.dsm` import, BMP export. No build step, no dependencies. |
| `ledsign/bmp.py` | 24-bit uncompressed BMP encode/decode/validate. Zero dependencies. |
| `ledsign/dsm.py` | Importer for the `.dsm` playlist authoring format. |
| `ledsign/drivers/base.py` | `SignDriver` — the interface every sign backend implements. |
| `ledsign/drivers/daystar.py` | Driver for DayStar / DBStar-family controllers (TCP 6006). |
| `docs/protocol-daystar.md` | The wire protocol, documented. |

## Supported signs

| Family | Transport | Read | Write | Notes |
|---|---|---|---|---|
| DayStar / DBStar | TCP 6006 | ✅ | ✅ | Playlist container synthesis is partial — see the protocol doc |

The driver layer exists so this table can grow. Adding a sign means implementing
`SignDriver`; nothing above that layer changes.

## The editor

Open `web/index.html` in a browser. That's it — no server, no build.

- **Drag any image** (PNG/JPG/GIF) onto the panel
- **Move it** by dragging, or nudge with arrow keys (shift = 8px)
- **Fit modes** — contain / cover / stretch / native, plus free scaling
- **Text layers** with font, size, color, bold
- **Side A / Side B**, or mirror both
- **Import `.dsm`** to pull in an existing playlist
- **Export BMP** at exact panel geometry

The canvas runs at *native sign resolution*, so the preview is literally the pixels that
get encoded — no scaling surprises between what you see and what ships.

## Python usage

```python
from ledsign import bmp
from ledsign.drivers.daystar import DayStarSign

# Read-only by default.
sign = DayStarSign("192.168.1.50")
print(sign.info())
for f in sign.list_files()[:10]:
    print(f.size, f.name)

# Back up before changing anything.
sign.backup("./backup")

# Writes require an explicit opt-in.
sign = DayStarSign("192.168.1.50", allow_writes=True)
frame = bmp.from_image("logo.png", 112, 32, fit="contain")
sign.put_file("logo.bmp", bmp.encode(frame))
```

Build a frame from scratch:

```python
from ledsign import bmp

f = bmp.Frame.blank(112, 32, (0, 0, 40))
logo = bmp.from_image("logo.png", 48, 24, fit="contain")
f.paste(logo, 4, 4)
open("frame.bmp", "wb").write(bmp.encode(f))
```

## Safety

These controllers are unauthenticated and cleartext, and on at least one family the **same
channel that programs content also carries firmware upload**. This library is built
accordingly:

- Drivers are **read-only unless constructed with `allow_writes=True`**
- **No firmware, provisioning, or network-reconfiguration opcode is implemented at all**
- `backup()` needs no write permission and should be run before any programming session

Point this at hardware you own or are authorized to operate.

## Contributing a sign

1. Capture the vendor app talking to your panel (`tcpdump`/Wireshark, filter on the sign's IP)
2. Document the framing in `docs/`
3. Implement `SignDriver` in `ledsign/drivers/`
4. Verify by downloading a frame and confirming it hash-matches the authoring PC's copy —
   that round-trip is what proves a driver is correct rather than merely plausible

## Roadmap

- Playlist container synthesis (currently template-based)
- **AVI / animation** — controllers store zlib-compressed frames and the vendor app wraps
  ffmpeg, so video is very likely a transcoded frame sequence rather than a container
  upload. Needs a capture of a video transmit to confirm.
- Discovery reply field layout
- More sign families

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or derived from any sign vendor's source code. Protocol
details were obtained by observing network traffic on hardware operated by the authors.
