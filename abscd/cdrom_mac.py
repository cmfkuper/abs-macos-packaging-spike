# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Optical drive access on macOS.

macOS mounts an audio CD via the cddafs filesystem: the disc appears under
/Volumes as one AIFF file per track ("1 Audio Track.aiff", ...), and the
kernel handles error correction. "Ripping" is therefore file reading —
each Track carries its source AIFF path (MacTrack) and engine.Ripper copies
it. Drive enumeration, disc detection, and eject go through drutil.

Every parser is a pure function taking text/bytes, so the whole layer is
testable off-Mac with fixtures, and a mismatch against real hardware output
is a one-function fix.

Loaded only on macOS (see cdrom.py, the platform facade).
"""

from __future__ import annotations

import hashlib
import re
import struct
import subprocess
import time
from pathlib import Path

from .cdrom import (SECTORS_PER_SECOND, DriveError, MacTrack, NoDiscError,
                    RawReadUnsupported, Toc)

_TRACK_NAME = re.compile(r"^(\d+)\b.*\.aiff?$", re.IGNORECASE)
_MOUNT_LINE = re.compile(r" on (/Volumes/.+?) \([^)]*cddafs")
_DRUTIL_DRIVE = re.compile(r"^\s*(\d+)\s", re.MULTILINE)


def _run(cmd: list[str], timeout: float = 15) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveError(f"{cmd[0]} failed: {exc}") from exc


# ---------- pure parsers (fixture-tested) ----------

def parse_mount_output(text: str) -> Path | None:
    """The cddafs mount point from `mount` output, or None if no audio CD."""
    m = _MOUNT_LINE.search(text)
    return Path(m.group(1)) if m else None


def parse_drutil_list(text: str) -> list[str]:
    """Drive numbers from `drutil list` output, e.g. ['1']."""
    return _DRUTIL_DRIVE.findall(text)


def parse_aiff_frames(header: bytes) -> int:
    """Sample-frame count from an AIFF file's COMM chunk.

    (The stdlib aifc module was removed in Python 3.13, so this is a minimal
    hand parser: FORM/AIFF container, chunks of [id, u32 size, payload].)
    """
    if len(header) < 12 or header[0:4] != b"FORM" or header[8:12] not in (b"AIFF", b"AIFC"):
        raise DriveError("Not an AIFF file")
    pos = 12
    while pos + 8 <= len(header):
        chunk_id = header[pos:pos + 4]
        (size,) = struct.unpack(">I", header[pos + 4:pos + 8])
        if chunk_id == b"COMM" and pos + 8 + 10 <= len(header):
            (_channels, frames) = struct.unpack(">hI", header[pos + 8:pos + 14])
            return frames
        pos += 8 + size + (size & 1)  # chunks are word-aligned
    raise DriveError("AIFF file has no COMM chunk")


def track_number_of(name: str) -> int | None:
    m = _TRACK_NAME.match(name)
    return int(m.group(1)) if m else None


# ---------- the drive ----------

def list_optical_drives() -> list[str]:
    try:
        drives = parse_drutil_list(_run(["drutil", "list"]))
    except DriveError:
        return []
    return drives or ["1"]  # drutil list can be terse; a Mac with drutil answering has a drive


class OpticalDrive:
    """One optical drive. macOS addresses the default drive implicitly through
    drutil; the identifier is kept for display and multi-drive systems."""

    def __init__(self, ident: str):
        self.ident = str(ident).strip() or "1"
        self.letter = self.ident  # name parity with the Windows backend (logs)

    def _mount_point(self) -> Path | None:
        return parse_mount_output(_run(["mount"]))

    def has_disc(self) -> bool:
        mp = self._mount_point()
        return mp is not None and mp.is_dir()

    def read_toc(self) -> Toc:
        mp = self._mount_point()
        if mp is None or not mp.is_dir():
            raise NoDiscError("No audio CD is mounted.")
        entries = []
        for f in mp.iterdir():
            n = track_number_of(f.name)
            if n is not None:
                entries.append((n, f))
        if not entries:
            raise NoDiscError(f"No audio tracks found in {mp}")
        entries.sort()

        tracks = []
        fingerprint = hashlib.sha1()
        for number, path in entries:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                frames = parse_aiff_frames(fh.read(64 * 1024))
            seconds = frames / 44100.0
            sectors = max(1, round(seconds * SECTORS_PER_SECOND))
            fingerprint.update(f"{number}:{size}:{frames};".encode())
            tracks.append(MacTrack(number=number, start_lba=0, end_lba=sectors,
                                   is_audio=True, source=str(path)))
        return Toc(tracks=tuple(tracks), disc_id=fingerprint.hexdigest()[:16])

    def read_track(self, *_args, **_kwargs):
        # Never used on macOS: engine.Ripper copies MacTrack.source instead.
        raise RawReadUnsupported("Raw reads are not used on macOS.")

    def eject(self) -> None:
        _run(["drutil", "eject"], timeout=30)
        # cddafs can take a moment to unmount; give the poller a clean slate
        deadline = time.time() + 10
        while time.time() < deadline and self.has_disc():
            time.sleep(0.5)
