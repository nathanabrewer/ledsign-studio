# DayStar / DBStar LED controller — TCP 6006 wire protocol

Reverse-engineered from packet captures of the vendor Windows application talking to a
112×32 RGB panel. Recovered independently of any vendor source or documentation.

**Verification.** Every claim marked CONFIRMED was validated byte-for-byte in both
directions: a frame downloaded from a sign and a frame uploaded to it each hash-match the
reference copy on the authoring PC. The upload-header timestamp decodes to the exact
wall-clock second the vendor app transmitted, which is what pinned that field layout.

Reference implementation: [`ledsign/drivers/daystar.py`](../ledsign/drivers/daystar.py).

---

## 1. Transport

- **TCP, port 6006.** Cleartext, unauthenticated.
- **UDP 6007** carries multicast discovery (see §6).
- **Client speaks first.** The controller sends nothing on connect — there is no banner or
  greeting. A bare connect-and-listen returns zero bytes and times out; this is normal and
  is not evidence of a broken link.
- No FTP is involved. Earlier analysis of the vendor binary suspected a secondary FTP
  transfer path; captures show all transfers ride 6006.

## 2. Frame header

Every message begins with a 12-byte header.

```
off  size  field
 0   1     0x73  's' magic
 1   1     sub-magic — 0x74 on reads, 0x04 on writes
 2   2     version (0x07 0x03 observed)
 4   1     flag / side selector
 5   1     opcode class: 0x02 path op · 0x03 short command · 0x04 start playlist
 6   2     reserved (0x0000)
 8   4     uint32 LE — selector (short command) or frame length (path op)
```

## 3. Reads  `[CONFIRMED]`

### 3.1 Directory listing
Request — 12 bytes:
```
73 74 07 03 00 03 00 00  04 00 00 00
```
Response begins with ASCII `star` + a `uint32`, then repeating records:
```
uint16 namelen · uint16 alloclen · char name[alloclen] · uint32 size · byte ts[6]
```

### 3.2 File read
A **round-trip is mandatory** between the two requests; sending them back-to-back resets
the connection.
```
→ 12 bytes   73 74 07 03 01 03 00 00  0c 00 00 00
← 16 bytes   preamble; bytes 4..11 are two float32 LE (panel temperatures)
→ 260 bytes  73 74 07 03 01 02 00 00  04 01 00 00     (0x104 = 260 = frame length)
             02 00 33 00
             <path> NUL-padded to 260
← 8 bytes    bytes 4..7 = uint32 LE file size
← <size> bytes of raw file
```
Path namespace observed: `/bitmaps/<name>.bmp`, `/sysdata/*.log`.

### 3.3 Info query
Returns a mixed ASCII/binary block: panel geometry, CPU model, firmware date, Linux kernel
string (used by the vendor app to distinguish controller generations), DNS servers,
`ip,netmask,gateway,mac`, and the path of the currently loaded playlist.

## 4. Writes  `[CONFIRMED]`

### 4.1 Upload header — 284 bytes
```
off  size  field
 0   8     73 04 07 03 02 02 00 00
 8   4     uint32 LE — payload size
12   4     file type: 02 00 02 00 = bitmap · 04 00 04 00 = playlist
16   2     uint16 seconds
18   2     uint16 minutes
20   2     uint16 hours
22   2     uint16 day
24   2     uint16 month
26   2     uint16 year - 1900
28   ...   target filename, NUL-padded to 284
```

### 4.2 Payload
```
8-byte sub-header  00 00 00 00 00 40 00 00      (0x4000 = 16384)
followed by the raw file bytes
```
Ack: `uint32 length · uint32 length · uint32 status` — length echoed twice, status 0 = OK.

### 4.3 Start playlist — 16 bytes
```
73 04 07 03 01 04 00 00  10 00 00 00  ff 00 4a 00
```

### 4.4 Transmit sequence
```
for each frame whose content differs from what the sign already holds:
    upload header → payload → ack
upload playlist container → payload → ack
start playlist → ack
```
The vendor app skips frames already resident with identical content — an unchanged
playlist uploads zero bitmaps and only rewrites the container.

### 4.5 Playlist container  `[PARTIAL]`
The container (`play-1.lst`) begins with a `uint32` entry count, then the source authoring
file path in a fixed NUL-padded field, then panel geometry as two `uint16`s, then a
parameters block, then per-entry records naming each bitmap in play order, then font
references.

The per-entry record layout is **not fully decoded**. This library therefore edits a
captured container rather than synthesising one; see `program_with_template()`.

## 5. Frame format

Frames are plain **24-bit uncompressed Windows BMPs** (`BI_RGB`, no palette) at panel
resolution — for a 112×32 panel, exactly **10806 bytes** (54-byte header + 112×32×3).
Rows are stored bottom-up and channels are BGR, per the BMP spec.

Controllers also store a **`.zlb`** sibling for each bitmap: raw zlib of the BMP bytes
(`78 5e` header), confirmed to decompress byte-identically. Compression on typical sign
art runs about **28:1**.

## 6. Discovery  `[PARTIAL]`

UDP multicast on port 6007. The probe is a 12-byte frame:
```
off 0     magic byte
off 1-3   minimum firmware version (min, mid, maj)
off 4-7   uint32 LE command word
off 8-11  uint32 LE parameter
```
An ASCII probe variant also exists. Controllers announce on a separate multicast group with
an ASCII payload containing at least their IP. Exact reply field order is unverified.
Multicast is link-local — discovery only works from the panel's own L2 segment.

## 7. Security notes

These controllers are designed for an isolated, trusted LAN:

- Discovery is unauthenticated; any host on the segment can enumerate panels.
- The control channel is cleartext with no authentication whatsoever.
- **The same framing carries firmware upload and provisioning-script upload.** A controller
  reachable from an untrusted network can be bricked or persistently compromised.
- Network reconfiguration is remotely reachable and can strand a panel.

Do not expose port 6006 beyond a trusted segment. This library blocks writes unless a
driver is explicitly constructed with `allow_writes=True`, and implements no firmware,
provisioning, or network-reconfiguration opcode at all.
