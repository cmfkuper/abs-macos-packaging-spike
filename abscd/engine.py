"""Rip-and-convert engine: WAV writing, job state, and ffmpeg M4B assembly."""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cdrom

SAMPLE_RATE = cdrom.SAMPLE_RATE
CREATE_NO_WINDOW = 0x08000000  # keep ffmpeg from flashing console windows under the GUI


class EngineError(Exception):
    pass


def sanitize(name: str) -> str:
    """Make a string safe to use as a Windows file or folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Untitled"


def locate_ffmpeg() -> str | None:
    """Find ffmpeg.exe: PATH first, then the standard winget install locations."""
    found = shutil.which("ffmpeg")
    if found:
        return found
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
    """Find VLC, used as a rip fallback for drives that refuse raw reads."""
    for candidate in (
        shutil.which("vlc"),
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


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
    year: str
    root: Path  # Output/Author/Title (Year)
    discs: list[DiscRecord] = field(default_factory=list)

    @property
    def work_dir(self) -> Path:
        return self.root / "_rip"

    @property
    def base_name(self) -> str:
        return f"{sanitize(self.author)}, {sanitize(self.title)}, {sanitize(self.year)}"

    @property
    def state_file(self) -> Path:
        return self.work_dir / "job.json"

    @classmethod
    def create(cls, output_root: Path, author: str, title: str, year: str) -> "Job":
        root = output_root / sanitize(author) / f"{sanitize(title)} ({sanitize(year)})"
        job = cls(author=author.strip(), title=title.strip(), year=year.strip(), root=root)
        existing = job.state_file
        if existing.is_file():
            try:
                saved = json.loads(existing.read_text(encoding="utf-8"))
                job.discs = [DiscRecord(**d) for d in saved.get("discs", [])]
            except (json.JSONDecodeError, TypeError, KeyError):
                job.discs = []
        return job

    def save(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "author": self.author, "title": self.title, "year": self.year,
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
                status(f"Disc {disc_number}: ripping track {index} of {len(tracks)}â¦")
            wav = disc_dir / f"Track {index:02d}.wav"

            def tick(n, _base=done):
                nonlocal done
                done += n
                if progress is not None:
                    progress(done, total_bytes)

            try:
                chunks = self.drive.read_track(
                    track, should_cancel=should_cancel, on_read_error=on_bad)
                completed = write_wav(wav, chunks, track.frames,
                                      progress=tick, should_cancel=should_cancel)
            except cdrom.RawReadUnsupported:
                if status is not None:
                    status(f"Disc {disc_number}: drive refused raw reads; using VLC for track {index}â¦")
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
        total_seconds = sum(d.seconds for d in job.discs)
        if status is not None:
            hours = total_seconds / 3600
            status(f"Converting {len(wavs)} tracks ({hours:.1f} h of audio) to M4Bâ¦")

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
            "-movflags", "+faststart",
            "-metadata", f"artist={job.author}",
            "-metadata", f"album_artist={job.author}",
            "-metadata", f"title={job.title}",
            "-metadata", f"album={job.title}",
            "-metadata", f"date={job.year}",
            "-metadata", "genre=Audiobook",
            "-f", "mp4",
            "-progress", "pipe:1", "-nostats",
            str(out),
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
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
            stderr = proc.stderr.read() if proc.stderr else ""
            proc.wait()
        if proc.returncode != 0:
            out.unlink(missing_ok=True)
            tail = "\n".join(stderr.strip().splitlines()[-8:])
            raise EngineError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
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
