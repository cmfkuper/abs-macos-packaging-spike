# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
"""Fixture tests for the cdrom_mac parsers — runnable on any platform.

The fixtures mirror documented macOS output. If real hardware disagrees,
paste the real output into the fixture, adjust ONE parser, re-run.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# import the parsers directly (the facade would pick the win backend off-mac)
sys.modules.setdefault("abscd", __import__("abscd"))
from abscd import cdrom  # noqa: E402
from abscd.cdrom_mac import (parse_aiff_frames, parse_drutil_list,  # noqa: E402
                             parse_mount_output, track_number_of)

MOUNT_WITH_CD = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
/dev/disk6s0 on /Volumes/Audio CD (cddafs, local, nodev, nosuid, read-only, noowners)
"""

MOUNT_NO_CD = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
"""

# `drutil list` on a Mac with one external USB drive
DRUTIL_LIST = """\
   Vendor   Product           Rev
 1 MATSHITA DVD-R   UJ-8A8    HA13
"""


def make_aiff(frames: int, channels: int = 2) -> bytes:
    """Minimal valid AIFF header: FORM/AIFF + COMM + empty SSND."""
    comm = struct.pack(">hIh10s", channels, frames, 16, b"\x40\x0e\xac\x44\x00" + b"\x00" * 5)
    chunks = b"COMM" + struct.pack(">I", len(comm)) + comm
    chunks += b"SSND" + struct.pack(">I", 8) + b"\x00" * 8
    return b"FORM" + struct.pack(">I", 4 + len(chunks)) + b"AIFF" + chunks


def main() -> int:
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")
        else:
            print(f"ok: {name} = {got!r}")

    check("mount with CD", parse_mount_output(MOUNT_WITH_CD), Path("/Volumes/Audio CD"))
    check("mount without CD", parse_mount_output(MOUNT_NO_CD), None)
    check("drutil list", parse_drutil_list(DRUTIL_LIST), ["1"])
    check("track number", track_number_of("1 Audio Track.aiff"), 1)
    check("track number 12", track_number_of("12 Audio Track.aiff"), 12)
    check("non-track file", track_number_of(".TOC.plist"), None)
    check("aiff frames", parse_aiff_frames(make_aiff(441000)), 441000)

    # a full 60-minute track header parses without reading the audio body
    check("aiff frames big", parse_aiff_frames(make_aiff(44100 * 3600)), 44100 * 3600)

    bad = b"RIFF" + b"\x00" * 40  # a WAV is not an AIFF
    try:
        parse_aiff_frames(bad)
        failures.append("aiff parser accepted a WAV header")
    except cdrom.DriveError:
        print("ok: WAV header rejected")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nMAC PARSER TESTS: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
