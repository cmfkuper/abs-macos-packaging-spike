"""macOS packaging spike — proves the .app bundle pipeline end to end.

The window does exactly three things: shows the bundled ffmpeg's version,
lists /Volumes (where audio CDs will appear via cddafs), and quits.
No CD code lives here yet.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

AUTO_QUIT_MS = 60_000  # close ourselves after a minute so CI/kiosk runs can't hang


def bundled_ffmpeg() -> Path | None:
    """Locate the ffmpeg binary that PyInstaller packed into this .app.

    PyInstaller 6 puts --add-binary items in different spots depending on
    onedir/onefile and .app layout, so probe every plausible location.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    exe_dir = Path(sys.executable).resolve().parent   # .app/Contents/MacOS
    roots += [
        exe_dir,
        exe_dir / "_internal",
        exe_dir.parent / "Frameworks",                # .app/Contents/Frameworks
        exe_dir.parent / "Resources",                 # .app/Contents/Resources
    ]
    for root in roots:
        candidate = root / "ffmpeg"
        if candidate.is_file():
            return candidate
    return None


def gather_report() -> tuple[bool, str]:
    """Returns (ok, text) — ok is False if the bundled ffmpeg is missing/broken."""
    lines = []
    ok = True

    ffmpeg = bundled_ffmpeg()
    if ffmpeg is None:
        ok = False
        lines.append("ffmpeg: NOT FOUND inside the app bundle")
    else:
        try:
            result = subprocess.run(
                [str(ffmpeg), "-version"], capture_output=True, text=True, timeout=30)
            banner = result.stdout.splitlines()
            lines.append(f"ffmpeg found: {ffmpeg}")
            lines.append(banner[0] if banner else "(no version output)")
            # second banner line names the copyright/license configuration
            if len(banner) > 1:
                lines.append(banner[1])
            if result.returncode != 0:
                ok = False
                lines.append(f"ffmpeg exited with code {result.returncode}")
        except OSError as exc:
            ok = False
            lines.append(f"ffmpeg would not execute: {exc}")

    lines.append("")
    volumes = Path("/Volumes")
    if volumes.is_dir():
        mounted = sorted(p.name for p in volumes.iterdir())
        lines.append(f"Mounted volumes ({len(mounted)}):")
        lines += [f"  • {name}" for name in mounted] or ["  (none)"]
    else:
        lines.append("/Volumes not found (not running on macOS?)")

    lines.append("")
    lines.append(f"Python {sys.version.split()[0]}  •  {sys.platform}")
    return ok, "\n".join(lines)


def run_gui() -> None:
    import tkinter as tk
    from tkinter import ttk

    ok, report = gather_report()

    root = tk.Tk()
    root.title("ABS macOS Packaging Spike")
    root.minsize(520, 360)

    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Packaging spike: " + ("PASS ✅" if ok else "FAIL ❌"),
              font=("Helvetica", 18, "bold")).pack(anchor="w")
    text = tk.Text(frame, height=16, width=64, borderwidth=0, highlightthickness=0)
    text.insert("1.0", report)
    text.configure(state="disabled")
    text.pack(fill="both", expand=True, pady=(12, 12))
    ttk.Button(frame, text="Quit", command=root.destroy).pack(anchor="e")

    root.after(AUTO_QUIT_MS, root.destroy)
    root.mainloop()


def main() -> None:
    if "--selftest" in sys.argv:
        # CI smoke test: no window, just prove the bundled ffmpeg executes.
        ok, report = gather_report()
        print(report)
        sys.exit(0 if ok else 1)
    run_gui()


if __name__ == "__main__":
    main()
