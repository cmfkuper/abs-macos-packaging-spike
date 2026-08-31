# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Direct optical-drive access on Windows via DeviceIoControl.

Everything here uses only ctypes, so there are no third-party packages to
install and no administrator rights are required: both IOCTL_CDROM_READ_TOC
and IOCTL_CDROM_RAW_READ work on a read-only device handle.

Loaded only on Windows (see cdrom.py, the platform facade).
"""

from __future__ import annotations

import ctypes
import hashlib
import string
from contextlib import contextmanager
from ctypes import wintypes

from .cdrom import (BYTES_PER_FRAME, RAW_SECTOR_SIZE, SAMPLE_RATE,
                    SECTORS_PER_SECOND, DriveError, NoDiscError,
                    RawReadUnsupported, Toc, Track)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DRIVE_CDROM = 5

IOCTL_CDROM_READ_TOC = 0x00024000
IOCTL_CDROM_RAW_READ = 0x0002403E
IOCTL_STORAGE_CHECK_VERIFY = 0x002D4800
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
IOCTL_STORAGE_LOAD_MEDIA = 0x002D480C

TRACK_MODE_CDDA = 2

# Errors that mean "this drive will never do a raw CDDA read", as opposed to
# "this particular sector was unreadable".
_UNSUPPORTED_ERRORS = {1, 50, 87, 1117}  # INVALID_FUNCTION, NOT_SUPPORTED, INVALID_PARAMETER, IO_DEVICE
_NO_MEDIA_ERRORS = {21, 1110, 1112, 1785}  # NOT_READY, MEDIA_CHANGED, NO_MEDIA, UNRECOGNIZED_MEDIA


kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetLogicalDrives.restype = wintypes.DWORD
kernel32.GetDriveTypeW.restype = wintypes.UINT
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]


class TRACK_DATA(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Reserved", ctypes.c_ubyte),
        ("ControlAdr", ctypes.c_ubyte),  # low nibble = Control, high nibble = Adr
        ("TrackNumber", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_ubyte),
        ("Address", ctypes.c_ubyte * 4),  # reserved, M, S, F
    ]


class CDROM_TOC(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Length", ctypes.c_ubyte * 2),  # big-endian, excludes itself
        ("FirstTrack", ctypes.c_ubyte),
        ("LastTrack", ctypes.c_ubyte),
        ("TrackData", TRACK_DATA * 100),
    ]


class RAW_READ_INFO(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("DiskOffset", ctypes.c_longlong),
        ("SectorCount", ctypes.c_ulong),
        ("TrackMode", ctypes.c_ulong),
    ]


def _msf_to_lba(addr) -> int:
    minutes, seconds, frames = addr[1], addr[2], addr[3]
    return ((minutes * 60 + seconds) * SECTORS_PER_SECOND + frames) - 150


def list_optical_drives() -> list[str]:
    """Return drive letters (e.g. ['E']) for every optical drive on the system."""
    mask = kernel32.GetLogicalDrives()
    found = []
    for i, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << i) and kernel32.GetDriveTypeW(letter + ":\\") == DRIVE_CDROM:
            found.append(letter)
    return found


class OpticalDrive:
    """A single optical drive, addressed by its letter."""

    def __init__(self, letter: str):
        self.letter = letter.strip().rstrip(":\\").upper()[:1]
        if not self.letter:
            raise ValueError("A drive letter is required")
        self.device_path = "\\\\.\\" + self.letter + ":"

    def __str__(self) -> str:
        return self.letter + ":"

    @contextmanager
    def _handle(self):
        handle = kernel32.CreateFileW(
            self.device_path, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            raise DriveError(f"Cannot open drive {self.letter}: (Windows error {err})")
        try:
            yield handle
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _ioctl(handle, code, in_buf=None, out_buf=None) -> int:
        returned = wintypes.DWORD(0)
        in_ptr = ctypes.byref(in_buf) if in_buf is not None else None
        in_len = ctypes.sizeof(in_buf) if in_buf is not None else 0
        out_ptr = ctypes.byref(out_buf) if out_buf is not None else None
        out_len = ctypes.sizeof(out_buf) if out_buf is not None else 0
        ok = kernel32.DeviceIoControl(
            handle, code, in_ptr, in_len, out_ptr, out_len, ctypes.byref(returned), None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return returned.value

    def has_disc(self) -> bool:
        """True if a disc is loaded and the drive is ready to read it."""
        try:
            with self._handle() as handle:
                self._ioctl(handle, IOCTL_STORAGE_CHECK_VERIFY)
            return True
        except (OSError, DriveError):
            return False

    def eject(self) -> None:
        try:
            with self._handle() as handle:
                self._ioctl(handle, IOCTL_STORAGE_EJECT_MEDIA)
        except (OSError, DriveError):
            pass  # a stuck or slot-loading tray is not worth failing the rip over

    def close_tray(self) -> None:
        try:
            with self._handle() as handle:
                self._ioctl(handle, IOCTL_STORAGE_LOAD_MEDIA)
        except (OSError, DriveError):
            pass

    def read_toc(self) -> Toc:
        """Read the table of contents of the loaded disc."""
        toc = CDROM_TOC()
        try:
            with self._handle() as handle:
                self._ioctl(handle, IOCTL_CDROM_READ_TOC, None, toc)
        except OSError as exc:
            if getattr(exc, "winerror", None) in _NO_MEDIA_ERRORS:
                raise NoDiscError(f"No readable disc in drive {self.letter}:") from exc
            raise DriveError(f"Could not read the disc table of contents: {exc}") from exc

        length = (toc.Length[0] << 8) | toc.Length[1]
        count = max(0, (length + 2 - 4) // 8)
        count = min(count, 100)

        entries = []
        for i in range(count):
            entry = toc.TrackData[i]
            entries.append((entry.TrackNumber, entry.ControlAdr & 0x0F, _msf_to_lba(entry.Address)))
        if len(entries) < 2:
            raise NoDiscError(f"Drive {self.letter}: reported no tracks (is it an audio CD?)")

        tracks = []
        for i, (number, control, lba) in enumerate(entries[:-1]):
            if number == 0xAA:
                continue
            tracks.append(Track(
                number=number,
                start_lba=lba,
                end_lba=entries[i + 1][2],
                is_audio=not (control & 0x04),
            ))
        if not any(t.is_audio for t in tracks):
            raise NoDiscError(f"The disc in {self.letter}: has no audio tracks")

        fingerprint = hashlib.sha1(
            ";".join(f"{t.number}:{t.start_lba}:{t.end_lba}" for t in tracks).encode()
        ).hexdigest()[:16]
        return Toc(tracks=tuple(tracks), disc_id=fingerprint)

    def read_track(self, track: Track, chunk_sectors: int = 26,
                   progress=None, should_cancel=None, on_read_error=None):
        """Yield raw CDDA bytes for one track.

        chunk_sectors defaults to 26 (26 * 2352 = 61,152 bytes) which stays under
        the 64 KB transfer size that some USB drives cap at.

        Unreadable sectors are retried, then filled with silence and reported
        through on_read_error rather than aborting a multi-disc job.
        """
        if not track.is_audio:
            raise DriveError(f"Track {track.number} is a data track and cannot be ripped as audio")

        first_read = True
        with self._handle() as handle:
            lba = track.start_lba
            while lba < track.end_lba:
                if should_cancel is not None and should_cancel():
                    return
                count = min(chunk_sectors, track.end_lba - lba)
                try:
                    data = self._raw_read(handle, lba, count)
                except RawReadUnsupported:
                    if first_read:
                        raise
                    data = None
                except OSError as exc:
                    if first_read and getattr(exc, "winerror", None) in _UNSUPPORTED_ERRORS:
                        raise RawReadUnsupported(str(exc)) from exc
                    data = None

                if data is None:
                    data = self._retry_read(handle, lba, count, on_read_error)
                first_read = False
                yield data
                if progress is not None:
                    progress(len(data))
                lba += count

    def _raw_read(self, handle, lba: int, count: int) -> bytes:
        info = RAW_READ_INFO(
            DiskOffset=lba * COOKED_SECTOR_SIZE,
            SectorCount=count,
            TrackMode=TRACK_MODE_CDDA,
        )
        out = ctypes.create_string_buffer(count * RAW_SECTOR_SIZE)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle, IOCTL_CDROM_RAW_READ, ctypes.byref(info), ctypes.sizeof(info),
            out, ctypes.sizeof(out), ctypes.byref(returned), None,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in _UNSUPPORTED_ERRORS:
                raise RawReadUnsupported(f"Windows error {err}")
            raise ctypes.WinError(err)
        return out.raw[:returned.value]

    def _retry_read(self, handle, lba: int, count: int, on_read_error) -> bytes:
        """Re-read a failed chunk sector by sector; substitute silence if hopeless."""
        pieces = []
        for offset in range(count):
            block = None
            for _ in range(8):
                try:
                    block = self._raw_read(handle, lba + offset, 1)
                    break
                except (OSError, RawReadUnsupported):
                    continue
            if block is None:
                block = b"\x00" * RAW_SECTOR_SIZE
                if on_read_error is not None:
                    on_read_error(lba + offset)
            pieces.append(block)
        return b"".join(pieces)
