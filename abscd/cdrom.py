# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Optical drive access: platform facade.

The neutral pieces live here — `Track`, `Toc`, the exception types, and the
CD audio constants. The actual drive backend is chosen by platform:

- Windows: cdrom_win.py (raw CDDA reads via DeviceIoControl)
- macOS:   cdrom_mac.py (cddafs mounts under /Volumes; ripping is file copy)

Callers import everything from this module and never touch a backend
directly, so the two implementations stay swappable behind one interface.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

RAW_SECTOR_SIZE = 2352      # bytes of audio per CD sector
SECTORS_PER_SECOND = 75
BYTES_PER_FRAME = 4         # 16-bit stereo
SAMPLE_RATE = 44100


class DriveError(Exception):
    """Any failure talking to the optical drive."""


class NoDiscError(DriveError):
    """No readable disc is currently loaded."""


class RawReadUnsupported(DriveError):
    """The drive rejected raw CDDA reads outright; caller should fall back."""


@dataclass(frozen=True)
class Track:
    number: int
    start_lba: int
    end_lba: int  # exclusive
    is_audio: bool

    @property
    def sectors(self) -> int:
        return max(0, self.end_lba - self.start_lba)

    @property
    def frames(self) -> int:
        """Number of 16-bit stereo sample frames in this track."""
        return self.sectors * RAW_SECTOR_SIZE // BYTES_PER_FRAME

    @property
    def seconds(self) -> float:
        return self.sectors / SECTORS_PER_SECOND

    @property
    def byte_size(self) -> int:
        return self.sectors * RAW_SECTOR_SIZE


@dataclass(frozen=True)
class MacTrack(Track):
    """A track backed by a cddafs AIFF file instead of raw sector ranges."""
    source: str = ""  # absolute path of the mounted AIFF


@dataclass(frozen=True)
class Toc:
    tracks: tuple[Track, ...]
    disc_id: str  # stable fingerprint, used to notice an unchanged disc

    @property
    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.is_audio]

    @property
    def seconds(self) -> float:
        return sum(t.seconds for t in self.audio_tracks)


if sys.platform == "win32":
    from .cdrom_win import OpticalDrive, list_optical_drives  # noqa: F401,E402
elif sys.platform == "darwin":
    from .cdrom_mac import OpticalDrive, list_optical_drives  # noqa: F401,E402
else:  # pragma: no cover — no backend for this platform
    def list_optical_drives() -> list[str]:
        return []

    class OpticalDrive:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs):
            raise DriveError("No optical drive backend for this platform.")
