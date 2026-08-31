# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
"""End-to-end engine test for the macOS path: AIFF tracks -> Ripper copy ->
Encoder -> chaptered, tagged m4b. Needs ffmpeg (set AUDIOBOOK_BOB_FFMPEG).

Runs the REAL Ripper and Encoder code; only the optical drive is absent —
tracks come as MacTrack fixtures pointing at synthesized AIFF files, exactly
the shape cdrom_mac.read_toc() produces.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abscd.cdrom import MacTrack, Toc  # noqa: E402
from abscd.engine import Encoder, Job, Ripper, locate_ffmpeg  # noqa: E402


def main() -> int:
    ffmpeg = locate_ffmpeg()
    assert ffmpeg, "ffmpeg not found (set AUDIOBOOK_BOB_FFMPEG)"
    print("ffmpeg:", ffmpeg)

    tmp = Path(tempfile.mkdtemp(prefix="bob-aiff-e2e-"))
    src = tmp / "volume"  # stands in for /Volumes/Audio CD
    src.mkdir()

    # two "discs" of two tracks each, distinct tones, honest AIFF via ffmpeg
    def make_track(n, seconds, freq):
        path = src / f"{n} Audio Track.aiff"
        subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                        "-t", str(seconds), "-c:a", "pcm_s16be", str(path)],
                       check=True)
        return path

    job = Job.create(tmp / "out", "E2E Author", "AIFF Book",
                     narrator="Test Narrator", edition="Abridged")

    ripper = Ripper(job, drive=None)  # the mac path never touches the drive
    for disc_no, freqs in ((1, (300, 400)), (2, (500, 600))):
        tracks = []
        for i, f in enumerate(freqs, start=1):
            p = make_track(i, 2.0, f)
            sectors = round(2.0 * 75)
            tracks.append(MacTrack(number=i, start_lba=0, end_lba=sectors,
                                   is_audio=True, source=str(p)))
        toc = Toc(tracks=tuple(tracks), disc_id=f"e2e{disc_no}")
        events = []
        rec = ripper.rip_disc(toc, disc_no,
                              progress=lambda d, t: events.append((d, t)),
                              status=lambda s: None)
        assert rec.tracks == 2 and len(rec.files) == 2, rec
        assert all(f.endswith(".aiff") for f in rec.files), rec.files
        # AIFF headers make actual bytes copied a hair over the sector-math
        # total; the UI clamps at 100%, so >=99% counts as completed here.
        assert events and events[-1][0] >= 0.99 * events[-1][1], "progress never completed"
        print(f"disc {disc_no}: copied {rec.files}")

    out = Encoder(job, bitrate="64k", channels=1).encode()
    print("m4b:", out, out.stat().st_size, "bytes")

    # verify with ffmpeg -i (the LGPL build has no ffprobe)
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(out)],
                           capture_output=True, text=True)
    info = probe.stderr
    checks = {
        "chapter 1": "Disc 1" in info,
        "chapter 2": "Disc 2" in info,
        "narrator tag": "Test Narrator" in info,
        "edition tag": "Abridged" in info,
        "aac audio": "aac" in info.lower(),
        "duration ~8s": "00:00:08" in info,
    }
    for name, ok in checks.items():
        print(("ok: " if ok else "MISSING: ") + name)
    if not all(checks.values()):
        print(info)
        return 1
    print("\nENGINE AIFF E2E: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
