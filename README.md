# Audiobook Bob

A streamlined replacement for Fre:ac: rips audiobook CDs one disc at a time and
assembles them into a single chaptered `.m4b` ready for Audiobookshelf.

## How to use it

1. Double-click **`Audiobook Bob.pyw`**.
2. Answer the three questions — **Author**, **Book title**, **Year published**.
3. Insert **Disc 1** and press **Start ripping**.
4. When the disc finishes it ejects automatically. Insert the next disc and press
   **Rip Disc N**, or press **That was the last disc — make the M4B**.
5. The finished file lands in:

   ```
   Output\<Author>\<Title> (<Year>)\<Title>.m4b
   ```

   Copy it into your Audiobookshelf library folder and scan.

## What it does for you

- Discs are ripped digit-perfect via raw CDDA reads (with automatic retries;
  an unreadable sector is patched with silence rather than killing the rip).
  If the drive refuses raw reads, it falls back to ripping with VLC.
- Each disc is labeled internally as `Author, Title, Year, Disc N` and becomes a
  **chapter** in the final M4B (`Disc 1`, `Disc 2`, …).
- Tags written to the M4B: artist/album-artist = author, title/album = book
  title, date = year, genre = Audiobook — exactly what Audiobookshelf reads.
- Audio is encoded to **AAC 96 kbps stereo** with `+faststart` for streaming.
- **Duplicate-disc guard**: if you accidentally re-insert a disc you already
  ripped, it tells you and ejects it.
- **Crash/resume safe**: ripped discs are kept in `Output\...\_rip\` until the
  M4B is built. If the program (or the PC) dies mid-book, start it again with
  the same author/title/year and it resumes at the next disc.

## Requirements (already installed)

- Python 3 (any recent version; uses only the standard library)
- ffmpeg (`winget install Gyan.FFmpeg`)
- VLC — optional, only used as a rip fallback

## Files

- `Audiobook Bob.pyw` — double-click launcher (no console window)
- `abscd/cdrom.py` — raw CD audio access (Windows DeviceIoControl, no drivers)
- `abscd/engine.py` — rip-to-WAV, job state/resume, ffmpeg M4B assembly
- `abscd/gui.py` — the window
