"""
daystar.py — driver for DayStar / DBStar-family LED sign controllers (TCP 6006).

Protocol recovered from packet captures of the vendor application and verified
byte-for-byte in both directions (a frame downloaded from a sign and a frame
uploaded to it both hash-match the reference copy on the authoring PC).

See docs/protocol-daystar.md for the wire format.

Status of each operation:
  info()             CONFIRMED
  list_files()       CONFIRMED
  get_file()         CONFIRMED
  put_file()         CONFIRMED
  start()            CONFIRMED
  playlist editing   CONFIRMED — insert/reorder/dwell, verified by reading the
                     container back off live hardware byte-identical
  program()          NOT SUPPORTED — building a container from nothing needs the
                     header block ahead of the entries, which is undecoded. Edit
                     the running container instead (get_playlist/add_frame).

Known limitation: records for frames carrying *dynamic* text (clock, temperature)
embed extra text-object and font sub-records, which shifts their parameter block.
playlist_entries() reads such an entry's dwell incorrectly. Static frames — the
overwhelming majority — are fine.

Video: these controllers accept video natively. A clip pulled off live hardware is
an ordinary AVI carrying H.264/yuv420p scaled to exactly panel resolution; the
host does NOT transcode to a frame sequence. Uploading video is not implemented
because the upload file-type constant for video is still unknown.

Frames may also be stored zlib-compressed (".zlb", confirmed raw zlib of the BMP,
about 28:1).
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

    # ------------------------------------------------- playlist container
    # Entries are edited by cloning an existing record and overwriting its name
    # and dwell. This sidesteps deriving the rule that sizes the name field: the
    # field is fixed-width within a container, so any name that fits works.
    # Verified against live hardware by inserting an entry and reading the
    # container back byte-identical.
    REC0 = 408          # offset of the first entry record
    NAME_OFF = 4        # name field, relative to record start
    # Parameter block follows the name field (which is NAME_FIELD bytes wide):
    #   +NAME_OFF+field+0   uint32  time span (86400 = all day)
    #   +NAME_OFF+field+8   uint32  end-date sentinel (INT32_MAX = no end)
    #   +NAME_OFF+field+12  uint32  dwell, milliseconds
    DWELL_REL = 12      # dwell uint32, relative to the end of the name field

    @classmethod
    def _stride(cls, container: bytes) -> int:
        """Distance between consecutive entry records (uniform within a container)."""
        marker = container[cls.REC0:cls.REC0 + 4]
        nxt = container.find(marker, cls.REC0 + 4)
        if nxt < 0:
            raise ValueError("could not determine entry stride")
        return nxt - cls.REC0

    @classmethod
    def _name_field(cls, container: bytes) -> int:
        return cls._stride(container) - 148

    @classmethod
    def playlist_entries(cls, container: bytes) -> list:
        """Return [(name, dwell_ms)] for a playlist container."""
        stride, out = cls._stride(container), []
        count = struct.unpack_from("<I", container, 0)[0]
        for i in range(count):
            r = cls.REC0 + i * stride
            if r + cls.NAME_OFF + 4 > len(container):
                break
            field = cls._name_field(container)
            name = container[r + cls.NAME_OFF:r + cls.NAME_OFF + field]
            name = name.split(b"\x00")[0].decode("ascii", "replace")
            dwell = struct.unpack_from(
                "<I", container, r + cls.NAME_OFF + field + cls.DWELL_REL)[0]
            out.append((name, dwell))
        return out

    @classmethod
    def playlist_insert(cls, container: bytes, name: str, dwell_ms: int = 3000,
                        index: int = -1, clone_from: int = 0) -> bytes:
        """Insert an entry, returning a new container.

        `name` must fit the container's name field (see `name_capacity`).
        `clone_from` picks which existing record to copy the parameter block from —
        keep it on a plain static frame, since records for clock/temperature frames
        carry extra embedded sub-records.
        """
        stride, field = cls._stride(container), cls._name_field(container)
        if len(name) + 1 > field:
            raise ValueError(
                f"name {name!r} needs {len(name)+1} bytes but the field is {field}; "
                f"use a shorter filename")
        src = cls.REC0 + clone_from * stride
        rec = bytearray(container[src:src + stride])
        rec[cls.NAME_OFF:cls.NAME_OFF + field] = name.encode() + b"\x00" * (field - len(name))
        struct.pack_into("<I", rec, cls.NAME_OFF + field + cls.DWELL_REL, int(dwell_ms))

        count = struct.unpack_from("<I", container, 0)[0]
        if index < 0 or index > count:
            index = count
        at = cls.REC0 + index * stride
        out = bytearray(container[:at]) + rec + bytearray(container[at:])
        struct.pack_into("<I", out, 0, count + 1)
        return bytes(out)

    @classmethod
    def playlist_set_dwell(cls, container: bytes, index: int, dwell_ms: int) -> bytes:
        out = bytearray(container)
        r = cls.REC0 + index * cls._stride(container)
        off = r + cls.NAME_OFF + cls._name_field(container) + cls.DWELL_REL
        struct.pack_into("<I", out, off, int(dwell_ms))
        return bytes(out)

    @classmethod
    def name_capacity(cls, container: bytes) -> int:
        """Longest filename (excluding the NUL) this container can hold."""
        return cls._name_field(container) - 1

    def get_playlist(self) -> bytes:
        return self.get_file("/playlist/play-1.lst")

    def put_playlist(self, container: bytes, *, start: bool = True) -> None:
        self._require_writes()
        self.put_file("play-1.lst", container, filetype=FILETYPE_PLAYLIST)
        if start:
            self.start()

    def add_frame(self, name: str, bmp_bytes: bytes, dwell_ms: int = 3000,
                  index: int = -1) -> bytes:
        """Upload a frame and add it to the running playlist. Returns the container."""
        self._require_writes()
        container = self.get_playlist()
        updated = self.playlist_insert(container, name, dwell_ms, index)
        self.put_file(name, bmp_bytes, filetype=FILETYPE_BITMAP)
        self.put_playlist(updated)
        return updated

    def program(self, playlist: Playlist) -> None:
        raise NotImplementedError(
            "Building a container from nothing is not supported; the header/parameter "
            "block ahead of the entry records is not fully decoded. Read the running "
            "container with get_playlist() and edit it with playlist_insert() / "
            "playlist_set_dwell(), or use add_frame()."
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
