# macOS Packaging Spike

Proves the distribution pipeline before any CD code is ported: GitHub Actions
builds a self-contained `.app` (Python + tkinter + static ffmpeg) that runs on
a completely clean Apple Silicon Mac.

## Running the build

Push this repo to GitHub, then either push any change under `spike/` or run
**Actions → macOS packaging spike → Run workflow**. When it finishes, download
the **ABS-Spike-macos-arm64** artifact from the run's Summary page.

## Running the .app on the Mac

1. Unzip the artifact. (It contains `ABS-Spike-macos-arm64.zip`; unzip that
   too — the inner zip is what preserves the executable permissions.)
2. The app is **ad-hoc signed, not notarized**, so Gatekeeper will refuse a
   plain double-click the first time. Two options:
   - Right-click the app → **Open** → **Open** (on newer macOS you may instead
     need System Settings → Privacy & Security → **Open Anyway**), or
   - In Terminal: `xattr -cr "ABS Spike.app"` then double-click normally.
3. A window opens showing **PASS** with the bundled ffmpeg's version banner and
   a list of everything mounted under `/Volumes`. It quits via the button (or
   auto-quits after 60 s).

If it shows PASS on a Mac with no Python/Homebrew/ffmpeg, the pipeline works
and the real port can reuse this exact packaging.

## What's inside

- `testapp.py` — the minimal window (also `--selftest` mode used by CI)
- ffmpeg: compiled **from official ffmpeg.org source in the workflow itself**
  (version pinned in the workflow's `FFMPEG_VERSION`), with a minimal
  **LGPL 2.1+** configuration — no `--enable-gpl`, no external libraries, only
  AAC encode, WAV/AIFF/PCM decode, and MP4/iPod muxing. CI asserts the
  `-L` banner says LGPL and fails the build if `--enable-gpl` ever appears.
  The compiled binary is cached, so it only rebuilds when the version bumps.
- Target: **arm64 only** (Apple Silicon). Intel Macs would need a second lane
  using an `x86_64` runner/binary — deliberately out of scope for the spike.
