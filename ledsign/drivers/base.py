"""
base.py — the interface every sign driver implements.

The goal of this layer is that the editor never knows what kind of sign it is
talking to. An editor produces frames (24-bit RGB bitmaps at the panel's
geometry) and a playlist; a driver knows how to get those onto one specific
family of hardware.

Adding a new sign means implementing SignDriver — nothing above this layer
should need to change.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Geometry:
    """Panel dimensions and capabilities."""
    width: int
    height: int
    sides: int = 1              # 1 = single-sided, 2 = Side A / Side B
    color: bool = True
    supports_compression: bool = False   # e.g. zlib-compressed frames
    supports_animation: bool = False


@dataclass
class RemoteFile:
    """One file as the sign reports it."""
    name: str
    size: int
    timestamp: bytes = b""


@dataclass
class PlaylistEntry:
    """One frame in a playlist."""
    name: str                   # filename as it will live on the sign
    data: bytes = b""           # encoded frame (BMP); empty if already resident
    dwell: float = 3.0          # seconds on screen
    speed: int = 30
    sides: tuple = (0,)         # which sides this entry targets


@dataclass
class Playlist:
    name: str = "playlist"
    entries: list = field(default_factory=list)

    def add(self, entry: PlaylistEntry) -> "Playlist":
        self.entries.append(entry)
        return self


class WriteBlocked(Exception):
    """Raised when a write is attempted without an explicit opt-in."""


class SignDriver(ABC):
    """Base class for a sign backend.

    Writes are opt-in per instance: construct with allow_writes=True to enable
    them. This is deliberate — these protocols are typically unauthenticated and
    the same channel that programs content often also carries firmware upload,
    so an accidental write can be expensive on real hardware.
    """

    name: str = "generic"

    def __init__(self, host: str, port: int, *, allow_writes: bool = False,
                 timeout: float = 20.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.allow_writes = allow_writes

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise WriteBlocked(
                "This driver is read-only. Construct it with allow_writes=True "
                "to permit writes to live hardware."
            )

    # -------------------------------------------------- required: reads
    @abstractmethod
    def geometry(self) -> Geometry: ...

    @abstractmethod
    def info(self) -> dict:
        """Identity/firmware/network details the sign reports about itself."""

    @abstractmethod
    def list_files(self) -> list:
        """Enumerate files resident on the sign."""

    @abstractmethod
    def get_file(self, path: str) -> bytes:
        """Download one file from the sign."""

    # -------------------------------------------------- required: writes
    @abstractmethod
    def put_file(self, name: str, data: bytes) -> None:
        """Upload one file. Must call self._require_writes() first."""

    @abstractmethod
    def program(self, playlist: Playlist) -> None:
        """Upload any changed frames, write the playlist, and start it."""

    # -------------------------------------------------- provided
    def backup(self, outdir: str, prefix: str = "") -> list:
        """Download every file (optionally name-filtered) to a local directory.

        Always available, never needs allow_writes, and is the thing to run
        before any programming session.
        """
        import os
        os.makedirs(outdir, exist_ok=True)
        saved = []
        for f in self.list_files():
            if prefix and not f.name.lower().startswith(prefix.lower()):
                continue
            data = self.get_file(self._remote_path(f.name))
            dest = os.path.join(outdir, f.name)
            with open(dest, "wb") as fh:
                fh.write(data)
            saved.append(dest)
        return saved

    def _remote_path(self, name: str) -> str:
        """Map a bare filename to the sign's path namespace."""
        return name

    def __repr__(self):
        mode = "rw" if self.allow_writes else "ro"
        return f"<{type(self).__name__} {self.host}:{self.port} [{mode}]>"
