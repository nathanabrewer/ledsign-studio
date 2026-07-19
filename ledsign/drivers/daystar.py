"""
daystar.py — driver for DayStar / DBStar-family LED sign controllers (TCP 6006).

Protocol recovered from packet captures of the vendor application and verified
byte-for-byte in both directions (a frame downloaded from a sign and a frame
uploaded to it both hash-match the reference copy on the authoring PC).

See docs/protocol-daystar.md for the wire format.

Status of each operation:
  info()        CONFIRMED
  list_files()  CONFIRMED
  get_file()    CONFIRMED
  put_file()    CONFIRMED
  start()       CONFIRMED
  program()     PARTIAL — the playlist container's per-entry record layout is
                only partially decoded, so playlists are produced by editing a
                captured template rather than synthesised from scratch. See
                `program_with_template`.

TODO: AVI / animation. The controller family stores zlib-compressed frames
(".zlb", confirmed: raw zlib of the BMP, ~28:1). The vendor app wraps ffmpeg
client-side, so video is almost certainly transcoded to a frame sequence rather
than uploaded as a container — but this is unverified and needs a capture of a
video transmit before it is implemented.
"""
from __future__ import annotations
import socket, struct, zlib, datetime

from .base import SignDriver, Geometry, RemoteFile, Playlist, WriteBlocked

MAGIC = b"\x73\x74"          # 's','t' — used on read frames
MAGIC_W = b"\x73\x04"        # 's',0x04 — used on write frames
VERSION = b"\x07\x03"

OP_SHORT = 0x03              # short control command
OP_PATH = 0x02               # path operation (read or write)
OP_START = 0x04              # start playlist

SEL_LIST = 0x04              # short-command selector: directory listing
SEL_READ = 0x0c              # short-command selector: precedes a file read

READ_FRAME_LEN = 260
WRITE_HEADER_LEN = 284
PAYLOAD_SUBHEADER = b"\x00\x00\x00\x00\x00\x40\x00\x00"

FILETYPE_BITMAP = b"\x02\x00\x02\x00"
FILETYPE_PLAYLIST = b"\x04\x00\x04\x00"

START_FRAME = bytes.fromhex("730407030104000010000000ff004a00")


class DayStarSign(SignDriver):
    name = "daystar"

    def __init__(self, host, port=6006, *, allow_writes=False, timeout=20.0,
                 width=112, height=32, sides=2):
        super().__init__(host, port, allow_writes=allow_writes, timeout=timeout)
        self._geom = Geometry(width=width, height=height, sides=sides, color=True,
                              supports_compression=True, supports_animation=False)

    def geometry(self) -> Geometry:
        return self._geom

    def _remote_path(self, name: str) -> str:
        return name if name.startswith("/") else f"/bitmaps/{name}"

    # ---------------------------------------------------------------- frames
    @staticmethod
    def _short(selector: int, flag: int = 0) -> bytes:
        return MAGIC + VERSION + bytes([flag, OP_SHORT, 0, 0]) + struct.pack("<I", selector)

    @staticmethod
    def _read_frame(path: str) -> bytes:
        f = bytearray(READ_FRAME_LEN)
        f[0:2] = MAGIC
        f[2:4] = VERSION
        f[4] = 0x01
        f[5] = OP_PATH
        struct.pack_into("<I", f, 8, READ_FRAME_LEN)
        f[12:14] = b"\x02\x00"
        f[14:16] = b"\x33\x00"
        p = path.encode("ascii")
        if len(p) > READ_FRAME_LEN - 16:
            raise ValueError("path too long")
        f[16:16 + len(p)] = p
        return bytes(f)

    @staticmethod
    def _write_header(name: str, size: int, filetype: bytes, when=None) -> bytes:
        """284-byte upload header.

        Bytes 16..27 are a timestamp as six uint16s (sec, min, hour, day, month,
        year-1900) — this decodes exactly to the moment the vendor app sends,
        which is what confirmed the field layout.
        """
        when = when or datetime.datetime.now()
        f = bytearray(WRITE_HEADER_LEN)
        f[0:2] = MAGIC_W
        f[2:4] = VERSION
        f[4] = 0x02
        f[5] = OP_PATH
        struct.pack_into("<I", f, 8, size)
        f[12:16] = filetype
        for i, v in enumerate((when.second, when.minute, when.hour,
                               when.day, when.month, when.year - 1900)):
            struct.pack_into("<H", f, 16 + i * 2, v)
        n = name.encode("ascii")
        if len(n) > WRITE_HEADER_LEN - 28:
            raise ValueError("filename too long")
        f[28:28 + len(n)] = n
        return bytes(f)

    # ---------------------------------------------------------------- socket
    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        return s

    @staticmethod
    def _recv_exact(s, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            try:
                b = s.recv(min(65536, n - len(buf)))
            except socket.timeout:
                break
            if not b:
                break
            buf += b
        return buf

    @staticmethod
    def _drain(s) -> bytes:
        buf = b""
        while True:
            try:
                b = s.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            buf += b
        return buf

    # ---------------------------------------------------------------- reads
    def get_file(self, path: str) -> bytes:
        """Download a file. A round-trip between the two requests is required —
        sending them back-to-back gets the connection reset."""
        path = self._remote_path(path)
        with self._connect() as s:
            s.sendall(self._short(SEL_READ, flag=1))
            hello = self._recv_exact(s, 16)
            if len(hello) < 16:
                raise IOError(f"short hello ({len(hello)} B)")
            self.last_temps = struct.unpack_from("<ff", hello, 4)
            s.sendall(self._read_frame(path))
            hdr = self._recv_exact(s, 8)
            if len(hdr) < 8:
                raise IOError(f"short size header ({len(hdr)} B)")
            size = struct.unpack_from("<I", hdr, 4)[0]
            data = self._recv_exact(s, size)
        if len(data) != size:
            raise IOError(f"short read: {len(data)} of {size}")
        return data

    def list_files(self) -> list:
        """Enumerate files on the sign.

        The listing arrives as a sequence of blocks, each introduced by a 'star'
        magic and a uint32 giving that block's total length (header included).
        Within a block, records are:

            uint16 namelen · uint16 alloclen · name[alloclen] · uint32 size · byte ts[6]

        where alloclen is namelen rounded up to a 4-byte boundary.
        """
        with self._connect() as s:
            s.sendall(self._short(SEL_LIST, flag=0))
            blob = self._drain(s)
        if not blob.startswith(b"star"):
            raise IOError(f"unexpected listing magic {blob[:8].hex(' ')}")

        out, pos = [], 0
        while pos + 8 <= len(blob):
            if blob[pos:pos + 4] != b"star":
                break                                   # end of blocks
            block_len = struct.unpack_from("<I", blob, pos + 4)[0]
            end = min(pos + block_len, len(blob)) if block_len else len(blob)
            off = pos + 8
            while off + 4 <= end:
                namelen, alloclen = struct.unpack_from("<HH", blob, off)
                if namelen == 0 or alloclen != ((namelen + 3) // 4) * 4:
                    break                               # not a valid record
                if off + 4 + alloclen + 10 > end:
                    break                               # truncated tail
                off += 4
                name = blob[off:off + namelen].split(b"\x00")[0].decode("ascii", "replace")
                off += alloclen
                size = struct.unpack_from("<I", blob, off)[0]; off += 4
                ts = blob[off:off + 6]; off += 6
                out.append(RemoteFile(name=name, size=size, timestamp=ts))
            if not block_len:
                break
            pos += block_len
        return out

    def info(self) -> dict:
        """Identity block. The sign returns a mixed ASCII/binary blob; the useful
        fields are extracted heuristically."""
        with self._connect() as s:
            s.sendall(self._short(0x03, flag=0))
            blob = self._drain(s)
        text = blob.decode("latin-1", "replace")
        out = {"raw_len": len(blob)}
        import re
        if m := re.search(r"(\d+)x(\d+)", text):
            out["width"], out["height"] = int(m.group(1)), int(m.group(2))
        if m := re.search(r"Linux \S+ (\S+)", text):
            out["kernel"] = m.group(1)
        if m := re.search(r"(\d+\.\d+\.\d+\.\d+),(\d+\.\d+\.\d+\.\d+),"
                          r"(\d+\.\d+\.\d+\.\d+)\s*,([0-9a-fA-F:]{17})", text):
            out.update(ip=m.group(1), netmask=m.group(2),
                       gateway=m.group(3), mac=m.group(4))
        if m := re.search(r"([A-Za-z]:\\[^\x00]+\.dsm)", text):
            out["loaded_playlist"] = m.group(1)
        return out

    # ---------------------------------------------------------------- writes
    def put_file(self, name: str, data: bytes, *, filetype: bytes = FILETYPE_BITMAP,
                 compress: bool = False) -> None:
        """Upload one file to the sign.

        compress=True sends a zlib-compressed frame (".zlb"). The controller
        stores both forms; compression is ~28:1 on typical sign art.
        """
        self._require_writes()
        if compress:
            data = zlib.compress(data)
            if not name.endswith(".zlb"):
                name = name.rsplit(".", 1)[0] + ".zlb"
        with self._connect() as s:
            s.sendall(self._write_header(name, len(data), filetype))
            s.sendall(PAYLOAD_SUBHEADER + data)
            ack = self._recv_exact(s, 12)
        if len(ack) >= 12:
            echoed = struct.unpack_from("<I", ack, 0)[0]
            status = struct.unpack_from("<I", ack, 8)[0]
            if echoed != len(data):
                raise IOError(f"sign echoed length {echoed}, sent {len(data)}")
            if status != 0:
                raise IOError(f"sign returned status {status}")

    def start(self) -> None:
        """Tell the sign to begin playing the currently programmed playlist."""
        self._require_writes()
        with self._connect() as s:
            s.sendall(START_FRAME)
            self._recv_exact(s, 13)

    def program(self, playlist: Playlist) -> None:
        raise NotImplementedError(
            "Synthesising a playlist container from scratch is not yet supported — "
            "its per-entry record layout is only partially decoded. Use "
            "program_with_template() with a playlist captured from the vendor app, "
            "or upload frames individually with put_file() and start()."
        )

    def program_with_template(self, template: bytes, frames: dict,
                              *, compress: bool = False) -> None:
        """Upload frames, then push a playlist container built from `template`.

        `template` must be a playlist file captured from the vendor app for a
        sign of this geometry. `frames` maps filename -> encoded BMP bytes; only
        frames whose content differs from what the sign already holds need to be
        included (the vendor app skips unchanged files entirely).
        """
        self._require_writes()
        resident = {f.name.lower(): f.size for f in self.list_files()}
        for name, data in frames.items():
            if resident.get(name.lower()) == len(data):
                continue          # already present at the same size
            self.put_file(name, data, filetype=FILETYPE_BITMAP, compress=compress)
        self.put_file("play-1.lst", template, filetype=FILETYPE_PLAYLIST)
        self.start()
