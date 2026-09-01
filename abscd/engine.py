# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Rip-and-convert engine: WAV writing, job state, and ffmpeg M4B assembly."""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cdrom

SAMPLE_RATE = cdrom.SAMPLE_RATE
# Windows-only Popen flag (POSIX Popen raises on nonzero creationflags)
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class EngineError(Exception):
    pass


def sanitize(name: str) -> str:
    """Make a string safe to use as a Windows file or folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Untitled"


def _bundled_ffmpeg() -> str | None:
    """ffmpeg packed inside a frozen app bundle (PyInstaller layouts vary)."""
    if not getattr(sys, "frozen", False):
        return None
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    exe_dir = Path(sys.executable).resolve().parent
    roots = [exe_dir, exe_dir / "_internal"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.insert(0, Path(meipass))
    if sys.platform == "darwin":
        roots += [exe_dir.parent / "Frameworks", exe_dir.parent / "Resources"]
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return str(candidate)
    return None


FFMPEG_MISSING_MSG = (
    "ffmpeg is required to build the M4B but was not found. "
    + ("Install it with:  winget install Gyan.FFmpeg  and restart."
       if sys.platform == "win32" else
       "This copy of Audiobook Bob is missing its bundled ffmpeg - "
       "please reinstall the app.")
)


def locate_ffmpeg() -> str | None:
    """Find ffmpeg: env override, app bundle, PATH, then winget locations."""
    override = os.environ.get("AUDIOBOOK_BOB_FFMPEG")
    if override and Path(override).is_file():
        return override
    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled
    found = shutil.which("ffmpeg")
    if found:
        return found
    if sys.platform != "win32":
        return None
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"]
    packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        candidates.extend(packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def locate_vlc() -> str | None:
    """Find VLC, used as a rip fallback for drives that refuse raw reads.
    Windows only: on macOS the kernel reads the disc, so no fallback exists."""
    if sys.platform != "win32":
        return None
    for candidate in (
        shutil.which("vlc"),
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _copy_with_progress(src: Path, dest: Path, progress=None,
                        should_cancel=None, chunk_size: int = 1 << 20) -> bool:
    """Copy a file in chunks with the same callback contract as write_wav.
    Returns False if cancelled (partial file removed)."""
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                if should_cancel is not None and should_cancel():
                    fout.close()
                    tmp.unlink(missing_ok=True)
                    return False
                chunk = fin.read(chunk_size)
                if not chunk:
                    break
                fout.write(chunk)
                if progress is not None:
                    progress(len(chunk))
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise EngineError(f"Could not copy {src.name}: {exc}") from exc
    tmp.replace(dest)
    return True


def write_wav(path: Path, chunks, expected_frames: int, progress=None, should_cancel=None) -> bool:
    """Stream raw CDDA chunks into a canonical 44.1 kHz 16-bit stereo WAV.

    Returns False if cancelled part-way (the partial file is removed).
    """
    data_size = expected_frames * cdrom.BYTES_PER_FRAME
    tmp = path.with_suffix(".partial")
    written = 0
    with open(tmp, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE")
        fh.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, SAMPLE_RATE,
                                       SAMPLE_RATE * 4, 4, 16))
        fh.write(b"data" + struct.pack("<I", data_size))
        for chunk in chunks:
            if should_cancel is not None and should_cancel():
                fh.close()
                tmp.unlink(missing_ok=True)
                return False
            fh.write(chunk)
            written += len(chunk)
            if progress is not None:
                progress(len(chunk))
        # Pad out anything a short final read left behind so the header is honest.
        if written < data_size:
            fh.write(b"\x00" * (data_size - written))
    tmp.replace(path)
    return True


@dataclass
class DiscRecord:
    number: int
    disc_id: str
    tracks: int
    seconds: float
    files: list[str] = field(default_factory=list)  # relative WAV paths, in play order


@dataclass
class Job:
    """One audiobook: metadata plus every disc ripped so far, persisted to disk."""

    author: str
    title: str
    narrator: str
    edition: str  # Unabridged | Abridged | Unknown
    root: Path  # Output/Author/Title
    discs: list[DiscRecord] = field(default_factory=list)

    @property
    def work_dir(self) -> Path:
        return self.root / "_rip"

    @property
    def base_name(self) -> str:
        return f"{sanitize(self.author)}, {sanitize(self.title)}"

    @property
    def state_file(self) -> Path:
        return self.work_dir / "job.json"

    @classmethod
    def create(cls, output_root: Path, author: str, title: str,
               narrator: str = "", edition: str = "Unknown") -> "Job":
        root = output_root / sanitize(author) / sanitize(title)
        job = cls(author=author.strip(), title=title.strip(),
                  narrator=narrator.strip(), edition=edition.strip() or "Unknown",
                  root=root)
        existing = job.state_file
        if existing.is_file():
            try:
                saved = json.loads(existing.read_text(encoding="utf-8"))
                job.discs = [DiscRecord(**d) for d in saved.get("discs", [])]
                # a resumed job keeps its saved narrator/edition unless the
                # user typed something fresh this time
                if not job.narrator:
                    job.narrator = saved.get("narrator", "")
                if job.edition == "Unknown":
                    job.edition = saved.get("edition", "Unknown")
            except (json.JSONDecodeError, TypeError, KeyError):
                job.discs = []
        return job

    def save(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "author": self.author, "title": self.title,
            "narrator": self.narrator, "edition": self.edition,
            "discs": [vars(d) for d in self.discs],
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def seen_disc(self, disc_id: str) -> DiscRecord | None:
        return next((d for d in self.discs if d.disc_id == disc_id), None)

    def cleanup(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


class Ripper:
    """Rips one disc at a time into the job's working directory."""

    def __init__(self, job: Job, drive: cdrom.OpticalDrive):
        self.job = job
        self.drive = drive
        self.bad_sectors = 0

    def rip_disc(self, toc: cdrom.Toc, disc_number: int,
                 progress=None, status=None, should_cancel=None) -> DiscRecord:
        """Rip every audio track of the loaded disc.

        progress(done_bytes, total_bytes) is called as data lands.
        status(text) receives human-readable updates.
        """
        disc_dir = self.job.work_dir / f"Disc {disc_number:02d}"
        disc_dir.mkdir(parents=True, exist_ok=True)
        tracks = toc.audio_tracks
        total_bytes = sum(t.byte_size for t in tracks)
        done = 0
        files: list[str] = []
        self.bad_sectors = 0

        def on_bad(_lba):
            self.bad_sectors += 1

        for index, track in enumerate(tracks, start=1):
            if should_cancel is not None and should_cancel():
                raise EngineError("Cancelled")
            if status is not None:
                status(f"Disc {disc_number}: ripping track {index} of {len(tracks)}…")
            source = getattr(track, "source", "")
            ext = "aiff" if source else "wav"
            wav = disc_dir / f"Track {index:02d}.{ext}"

            def tick(n, _base=done):
                nonlocal done
                done += n
                if progress is not None:
                    progress(done, total_bytes)

            if source:
                # macOS: the kernel already error-corrected the audio into a
                # mounted AIFF; ripping is a plain copy with progress.
                completed = _copy_with_progress(
                    Path(source), wav, progress=tick, should_cancel=should_cancel)
                if not completed:
                    raise EngineError("Cancelled")
                files.append(str(wav.relative_to(self.job.work_dir)))
                continue

            try:
                chunks = self.drive.read_track(
                    track, should_cancel=should_cancel, on_read_error=on_bad)
                completed = write_wav(wav, chunks, track.frames,
                                      progress=tick, should_cancel=should_cancel)
            except cdrom.RawReadUnsupported:
                if status is not None:
                    status(f"Disc {disc_number}: drive refused raw reads; using VLC for track {index}…")
                completed = self._rip_with_vlc(track, wav)
                done = sum(t.byte_size for t in tracks[:index])
                if progress is not None:
                    progress(done, total_bytes)
            if not completed:
                raise EngineError("Cancelled")
            files.append(str(wav.relative_to(self.job.work_dir)))

        record = DiscRecord(
            number=disc_number, disc_id=toc.disc_id, tracks=len(tracks),
            seconds=toc.seconds, files=files,
        )
        self.job.discs = [d for d in self.job.discs if d.number != disc_number]
        self.job.discs.append(record)
        self.job.discs.sort(key=lambda d: d.number)
        self.job.save()
        return record

    def _rip_with_vlc(self, track: cdrom.Track, wav: Path) -> bool:
        vlc = locate_vlc()
        if not vlc:
            raise EngineError(
                "This drive does not support raw audio reads and VLC was not found "
                "to use as a fallback.")
        cmd = [
            vlc, "-I", "dummy", "--no-video", f"cdda:///{self.drive.letter}:/",
            f"--cdda-track={track.number}",
            "--sout", "#transcode{acodec=s16l,channels=2,samplerate=44100}"
                      ":std{access=file,mux=wav,dst=" + str(wav) + "}",
            "vlc://quit",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=1800,
                                creationflags=CREATE_NO_WINDOW)
        if result.returncode != 0 or not wav.is_file() or wav.stat().st_size < 1024:
            raise EngineError(f"VLC failed to rip track {track.number} from the disc.")
        return True


class Encoder:
    """Assembles all ripped discs, in order, into a single chaptered M4B."""

    def __init__(self, job: Job, bitrate: str = "96k", channels: int = 2,
                 cover: Path | None = None):
        self.job = job
        self.bitrate = bitrate
        self.channels = channels
        self.cover = cover if (cover and cover.is_file()) else None
        self.ffmpeg = locate_ffmpeg()
        if not self.ffmpeg:
            raise EngineError(
                "ffmpeg was not found. Install it (winget install Gyan.FFmpeg) and retry.")

    def output_path(self) -> Path:
        return self.job.root / f"{sanitize(self.job.title)}.m4b"

    @staticmethod
    def _ensure_not_in_use(out: Path) -> None:
        """Refuse to write an output file another process has open (e.g. an
        orphaned encoder): two writers interleave and destroy the file."""
        if sys.platform != "win32" or not out.exists():
            return
        import ctypes
        GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
        OPEN_EXISTING = 3
        handle = ctypes.windll.kernel32.CreateFileW(
            str(out), GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if handle in (-1, ctypes.c_void_p(-1).value):
            raise EngineError(
                f"The output file is in use by another program: {out}. "
                "Close it (or wait for the other encode to finish) and retry.")
        ctypes.windll.kernel32.CloseHandle(handle)

    def _verify_output(self, out: Path, expected_seconds: float) -> str | None:
        """None if the file's duration matches what was encoded, else the
        mismatch. Uses ffmpeg -i (the bundled build has no ffprobe)."""
        try:
            probe = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-i", str(out)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not probe the file ({exc})"
        m = re.search(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)", probe.stderr)
        if not m:
            return "no duration in the file"
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        if abs(duration - expected_seconds) > 10:
            return (f"file is {duration/3600:.2f} h but "
                    f"{expected_seconds/3600:.2f} h were encoded")
        return None

    def encode(self, progress=None, status=None, should_cancel=None) -> Path:
        job = self.job
        if not job.discs:
            raise EngineError("No discs have been ripped yet.")

        wavs: list[Path] = []
        for disc in sorted(job.discs, key=lambda d: d.number):
            for rel in disc.files:
                wav = job.work_dir / rel
                if not wav.is_file():
                    raise EngineError(f"Missing ripped file: {wav}")
                wavs.append(wav)

        concat = job.work_dir / "concat.txt"
        concat.write_text(
            "".join("file '" + str(w).replace("'", "'\\''") + "'\n" for w in wavs),
            encoding="utf-8")

        metadata = job.work_dir / "metadata.txt"
        metadata.write_text(self._ffmetadata(), encoding="utf-8")

        out = self.output_path()
        self._ensure_not_in_use(out)
        total_seconds = sum(d.seconds for d in job.discs)
        if status is not None:
            hours = total_seconds / 3600
            status(f"Converting {len(wavs)} tracks ({hours:.1f} h of audio) to M4B…")

        cmd = [
            self.ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-i", str(metadata),
        ]
        if self.cover is not None:
            cmd += ["-i", str(self.cover)]
        cmd += [
            "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
            "-c:a", "aac", "-b:a", self.bitrate, "-ac", str(self.channels),
        ]
        if self.cover is not None:
            # JPEG passes through untouched; anything else becomes MJPEG.
            is_jpeg = self.cover.read_bytes()[:2] == bytes([0xFF, 0xD8])
            cmd += ["-map", "2:v", "-disposition:v:0", "attached_pic"]
            cmd += ["-c:v", "copy"] if is_jpeg else ["-c:v", "mjpeg", "-q:v", "3"]
        cmd += [
            "-movflags", "+faststart+use_metadata_tags",  # freeform tags (EDITION) survive
            "-metadata", f"artist={job.author}",
            "-metadata", f"album_artist={job.author}",
            "-metadata", f"title={job.title}",
            "-metadata", f"album={job.title}",
            "-metadata", "genre=Audiobook",
        ]
        if job.narrator:
            # Audiobookshelf reads narrators from the composer tag
            # (server/scanner/AudioFileScanner.js: tagComposer -> narrators)
            cmd += ["-metadata", f"composer={job.narrator}"]
        # ABS has no audio-tag mapping for abridged status; EDITION is a
        # freeform atom for future tooling, and an Abridged book also gets a
        # visible subtitle since ABS does read tagSubtitle.
        cmd += ["-metadata", f"EDITION={job.edition}"]
        if job.edition.lower() == "abridged":
            cmd += ["-metadata", "subtitle=Abridged"]
        cmd += [
            "-f", "mp4",
            "-progress", "pipe:1", "-nostats",
            str(out),
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)

        # Drain stderr concurrently. With hundreds of concat inputs ffmpeg
        # writes enough stderr chatter to fill the ~64KB pipe buffer; left
        # undrained it blocks ffmpeg mid-write and deadlocks the whole
        # assembly (seen at 324 tracks: both processes idle at 0% CPU).
        import collections
        import threading
        stderr_tail: collections.deque = collections.deque(maxlen=80)

        def _drain():
            for err_line in proc.stderr:
                stderr_tail.append(err_line.rstrip())

        drain = threading.Thread(target=_drain, daemon=True)
        drain.start()
        try:
            for line in proc.stdout:
                if should_cancel is not None and should_cancel():
                    proc.kill()
                    out.unlink(missing_ok=True)
                    raise EngineError("Cancelled")
                if line.startswith("out_time_us=") and progress is not None:
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                        progress(min(seconds, total_seconds), total_seconds)
                    except ValueError:
                        pass
        finally:
            proc.wait()
            drain.join(timeout=10)
            stderr = chr(10).join(stderr_tail)
        if proc.returncode != 0:
            out.unlink(missing_ok=True)
            tail = "\n".join(stderr.strip().splitlines()[-8:])
            raise EngineError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
        problem = self._verify_output(out, total_seconds)
        if problem is not None:
            # Do NOT delete anything: the ripped discs stay on disk and
            # the bad file stays for inspection. Losing sources to a bad
            # encode must be impossible.
            raise EngineError(
                f"The finished file failed verification: {problem}. "
                "The ripped discs were kept; fix the problem and assemble again.")
        return out

    def _ffmetadata(self) -> str:
        """FFMETADATA1 document with one chapter per disc."""
        def esc(text: str) -> str:
            for ch in "\\=;#":
                text = text.replace(ch, "\\" + ch)
            return text

        lines = [";FFMETADATA1"]
        position_ms = 0
        for disc in sorted(self.job.discs, key=lambda d: d.number):
            start = position_ms
            position_ms += round(disc.seconds * 1000)
            lines += [
                "[CHAPTER]", "TIMEBASE=1/1000",
                f"START={start}", f"END={position_ms}",
                f"title={esc(f'Disc {disc.number}')}",
            ]
        return "\n".join(lines) + "\n"


def wait_for_disc(drive: cdrom.OpticalDrive, should_cancel=None,
                  poll_seconds: float = 2.0) -> cdrom.Toc:
    """Block until an audio disc is readable in the drive (or cancellation)."""
    while True:
        if should_cancel is not None and should_cancel():
            raise EngineError("Cancelled")
        if drive.has_disc():
            try:
                return drive.read_toc()
            except cdrom.NoDiscError:
                pass
        time.sleep(poll_seconds)
