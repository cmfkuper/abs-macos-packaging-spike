"""pywebview front end. Same worker/queue threading model as the tkinter
version: the rip runs on a daemon thread and communicates only through the
event queue; a dispatcher thread pushes each event to JS via evaluate_js.

Python -> JS: window.evaluate_js("onEvent({...})")  (push, throttled)
JS -> Python: window.pywebview.api.*                (js_api bridge)
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path

import webview

import shutil

from . import cdrom, engine, metadata
from .settings import QUALITY_TIERS, Settings, check_output_folder

PROGRESS_MIN_INTERVAL = 0.1  # seconds between progress pushes to the bridge

# The CRT design's outer window size at scale 1.0 (CSS content plus the
# frameless window chrome overhead measured on WebView2).
BASE_W, BASE_H = 1166, 826
MIN_SCALE = 0.7

TOO_SMALL_HTML = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Audiobook Bob</title></head>
<body style='font-family:Segoe UI,sans-serif;font-size:16px;margin:2rem'>
<h2 style='margin:0 0 .5rem'>This screen is too small for Audiobook Bob.</h2>
<p>The window needs roughly 1170 &times; 830 logical pixels, and this display
(after Windows display scaling) has less than 70% of that.</p>
<p>Lowering the display scaling in Windows Settings &rarr; System &rarr;
Display usually frees up enough room.</p>
</body></html>"""


def _work_area():
    """Primary monitor's usable rectangle (excludes the taskbar), in the same
    logical coordinate space pywebview positions windows in. Windows only."""
    if sys.platform != "win32":
        s = webview.screens[0]
        return 0, 0, s.width, s.height
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top



class JsApi:
    """The only object exposed to JavaScript. Kept deliberately tiny so
    pywebview's API introspection never walks Backend internals (exposing
    Backend directly makes it recurse into the native window object)."""

    def __init__(self, backend: "Backend"):
        self._backend = backend

    def get_init(self):
        return self._backend.get_init()

    def start_job(self, author, title, year, drive):
        return self._backend.start_job(author, title, year, drive)

    def answer(self, value):
        return self._backend.answer(value)

    def choose_output_folder(self):
        return self._backend.choose_output_folder()

    def set_audio_quality(self, tier):
        return self._backend.set_audio_quality(tier)

    def search_books(self, title, author):
        return self._backend.search_books(title, author)

    def select_result(self, index):
        return self._backend.select_result(index)

    def clear_selection(self):
        return self._backend.clear_selection()

    def minimize(self):
        return self._backend.minimize()

    def close_window(self):
        return self._backend.close_window()


class Backend:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.window: webview.Window | None = None
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self.disc_answer: queue.Queue = queue.Queue()
        self.job: engine.Job | None = None
        self._close_armed_until = 0.0
        self.ui_scale = 1.0
        self.search_results: list = []
        self.pending_cover = None  # Path of the cached, user-chosen cover

    # ---------- output folder ----------

    @property
    def output_root(self) -> Path:
        return self.settings.output_root

    def _output_status(self) -> dict:
        """Current destination + usability. First-ever launch auto-creates the
        historical default (Output/ next to the app) so behavior is unchanged."""
        root = self.settings.output_root
        if not self.settings.has("output_root"):
            # First-ever launch: auto-create the historical default so nothing
            # changes for existing behavior.
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        error = check_output_folder(root)
        return {"output_root": str(root), "output_ok": error is None,
                "output_error": error or ""}

    def choose_output_folder(self):
        """Native folder picker; validate, persist, and report."""
        result = self.window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=str(self.settings.output_root))
        if not result:
            return self._output_status()  # cancelled — report current state
        chosen = Path(result[0])
        error = check_output_folder(chosen)
        if error is not None:
            current = self._output_status()
            current["output_error"] = error
            return current
        self.settings.output_root = chosen
        self.settings.save()
        return self._output_status()

    # ---------- JS -> Python API (exposed as window.pywebview.api) ----------

    def get_init(self):
        return {"drives": cdrom.list_optical_drives(),
                "audio_quality": self.settings.audio_quality,
                "ui_scale": self.ui_scale,
                **self._output_status()}

    def set_audio_quality(self, tier: str):
        if tier not in QUALITY_TIERS:
            return {"ok": False, "error": f"Unknown quality tier: {tier}"}
        self.settings.audio_quality = tier
        self.settings.save()
        return {"ok": True, "audio_quality": tier}

    # ---------- metadata lookup (screen 1, optional) ----------

    def search_books(self, title: str, author: str):
        title, author = (title or "").strip(), (author or "").strip()
        if not title:
            return {"ok": False, "error": "Type a title first."}
        try:
            results = metadata.search(title, author)
        except Exception:  # noqa: BLE001 — lookup is optional, never blocks
            return {"ok": False,
                    "error": "Couldn't reach the book services — "
                             "your typed details will be used."}
        metadata.fetch_thumbs(results)
        self.search_results = results
        self.pending_cover = None
        return {"ok": True, "results": [
            {"title": r["title"], "author": r["author"], "year": r["year"],
             "edition": r["edition"], "thumb": r["thumb"], "source": r["source"]}
            for r in results]}

    def select_result(self, index: int):
        try:
            result = self.search_results[int(index)]
        except (IndexError, ValueError, TypeError):
            return {"ok": False, "error": "That result is no longer available."}
        self.pending_cover = metadata.download_cover(result)
        cover_data = ""
        if self.pending_cover is not None:
            try:
                cover_data = metadata._data_uri(self.pending_cover.read_bytes())
            except OSError:
                self.pending_cover = None
        return {"ok": True, "title": result["title"], "author": result["author"],
                "year": result["year"], "cover_data": cover_data}

    def clear_selection(self):
        self.pending_cover = None
        return True

    def start_job(self, author: str, title: str, year: str, drive: str):
        author, title, year = author.strip(), title.strip(), year.strip()
        drive = (drive or "").strip().rstrip(":")
        if self.worker and self.worker.is_alive():
            return {"ok": False, "error": "A rip is already in progress."}
        if not author or not title or not year:
            return {"ok": False,
                    "error": "Please fill in the author, book title, and date recorded."}
        if not drive:
            return {"ok": False, "error": "No optical drive was found on this computer."}
        if not engine.locate_ffmpeg():
            return {"ok": False, "error":
                    "ffmpeg is required to build the M4B but was not found. "
                    "Install it with:  winget install Gyan.FFmpeg  and restart."}
        folder_error = check_output_folder(self.output_root)
        if folder_error is not None:
            return {"ok": False, "error": folder_error + " — choose a new folder."}

        self.job = engine.Job.create(self.output_root, author, title, year)
        if self.pending_cover is not None:
            # Stash the chosen cover with the job so a crash/resume keeps it.
            try:
                self.job.work_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.pending_cover,
                                self.job.work_dir / "cover.img")
            except OSError:
                pass
        self.cancel_flag.clear()
        while not self.disc_answer.empty():  # drop stale answers from a prior run
            self.disc_answer.get_nowait()
        self.worker = threading.Thread(
            target=self._run_job, args=(cdrom.OpticalDrive(drive),), daemon=True)
        self.worker.start()
        return {"ok": True}

    def answer(self, value: str):
        """'next' / 'assemble' from the disc buttons, 'cancel' from the dialog."""
        if value == "cancel":
            self.cancel_flag.set()
        self.disc_answer.put(value)
        return True

    # ---------- window lifecycle ----------

    def minimize(self):
        if self.window is not None:
            self.window.minimize()
        return True

    def close_window(self):
        """The frameless window's in-page ✕. Same double-press guard as the
        native close: first press warns while a rip is running."""
        if self.worker and self.worker.is_alive():
            now = time.monotonic()
            if now >= self._close_armed_until:
                self._close_armed_until = now + 10
                self._emit("log", text="⚠ A rip is in progress — press ✕ again "
                                       "within 10 seconds to abort and quit.")
                return False
            self.cancel_flag.set()
            self.disc_answer.put("cancel")
        if self.window is not None:
            self.window.destroy()
        return True

    def on_closing(self):
        """Veto the first close while ripping; a second click within 10s quits."""
        if not (self.worker and self.worker.is_alive()):
            return True
        now = time.monotonic()
        if now < self._close_armed_until:
            self.cancel_flag.set()
            self.disc_answer.put("cancel")
            return True
        self._close_armed_until = now + 10
        self._emit("log", text="⚠ A rip is in progress — close again within "
                               "10 seconds to abort and quit.")
        return False

    # ---------- Python -> JS event pump ----------

    def _emit(self, kind: str, **payload):
        self.events.put((kind, payload))

    def dispatch_forever(self):
        """Drain the event queue into evaluate_js. Coalesces progress bursts:
        only the newest progress event is pushed, at most every 100 ms."""
        last_progress_push = 0.0
        while True:
            kind, payload = self.events.get()
            if kind == "progress":
                # Collapse any queued-up progress events into the latest one.
                try:
                    while True:
                        nk, np = self.events.queue[0], None
                        if nk[0] != "progress":
                            break
                        kind, payload = self.events.get_nowait()
                except (IndexError, queue.Empty):
                    pass
                now = time.monotonic()
                if now - last_progress_push < PROGRESS_MIN_INTERVAL \
                        and payload.get("done", 0) < payload.get("total", 1):
                    continue
                last_progress_push = now
            self._push(kind, payload)

    def _push(self, kind: str, payload: dict):
        if self.window is None:
            return
        event = json.dumps({"kind": kind, **payload})
        try:
            self.window.evaluate_js(f"window.onEvent({event})")
        except Exception:
            pass  # window closing; nothing sensible to do

    # ---------- worker thread (logic identical to the tkinter version) ----------

    def _run_job(self, drive: cdrom.OpticalDrive):
        job = self.job
        cancelled = self.cancel_flag.is_set
        try:
            ripper = engine.Ripper(job, drive)
            resumed = sorted(d.number for d in job.discs)
            for d in job.discs:
                self._emit("disc_status", number=d.number, state="done", tracks=d.tracks)
            if resumed:
                self._emit("log", text=f"Found discs {resumed} already ripped — resuming.")
            disc_number = (max(resumed) + 1) if resumed else 1

            while True:
                self._emit("stage", text=f"Insert Disc {disc_number} and close the tray…")
                self._emit("progress", done=0, total=1)
                toc = engine.wait_for_disc(drive, should_cancel=cancelled)

                seen = job.seen_disc(toc.disc_id)
                if seen is not None:
                    self._emit("log", text=(
                        f"⚠ This looks like Disc {seen.number}, which is already ripped. "
                        f"Swap in Disc {disc_number}."))
                    drive.eject()
                    self._wait_for_removal(drive, cancelled)
                    continue

                minutes = toc.seconds / 60
                self._emit("stage",
                           text=f"{job.base_name}, Disc {disc_number} — "
                                f"{len(toc.audio_tracks)} tracks, {minutes:.0f} min")
                self._emit("disc_status", number=disc_number, state="ripping", tracks=0)
                record = ripper.rip_disc(
                    toc, disc_number,
                    progress=lambda d, t: self._emit("progress", done=d, total=t),
                    status=lambda s: self._emit("stage", text=s),
                    should_cancel=cancelled,
                )
                note = f" ({ripper.bad_sectors} unreadable sectors patched)" \
                    if ripper.bad_sectors else ""
                self._emit("disc_status", number=record.number, state="done",
                           tracks=record.tracks)
                self._emit("log", text=f"✔ Disc {record.number} ripped — "
                                       f"{record.tracks} tracks{note}")
                drive.eject()
                disc_number += 1

                self._emit("ask_disc", next_number=disc_number)
                answer = self.disc_answer.get()
                if answer == "cancel":
                    raise engine.EngineError("Cancelled")
                if answer == "assemble":
                    break

            tier = QUALITY_TIERS[self.settings.audio_quality]
            cover = job.work_dir / "cover.img"
            encoder = engine.Encoder(
                job, bitrate=tier["bitrate"], channels=tier["channels"],
                cover=cover if cover.is_file() else None)
            self._emit("assembling", path=str(encoder.output_path),
                       discs=len(job.discs), has_cover=cover.is_file())
            self._emit("stage", text="Assembling discs and converting to M4B…")
            self._emit("progress", done=0, total=1)
            out = encoder.encode(
                progress=lambda d, t: self._emit("progress", done=d, total=t),
                status=lambda s: self._emit("stage", text=s),
                should_cancel=cancelled,
            )
            size_mb = out.stat().st_size / (1024 * 1024)
            job.cleanup()
            self._emit("log", text=f"✔ Wrote {out.name} ({size_mb:.0f} MB)")
            self._emit("finished", path=str(out), size_mb=round(size_mb))
        except engine.EngineError as exc:
            if str(exc) == "Cancelled":
                self._emit("aborted", text="Cancelled. Ripped discs were kept — "
                                           "the same book resumes where it left off.")
            else:
                self._emit("aborted", text=f"Error: {exc}")
        except cdrom.DriveError as exc:
            self._emit("aborted", text=f"Drive error: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface anything to the UI
            self._emit("aborted", text=f"Unexpected error: {exc!r}")

    @staticmethod
    def _wait_for_removal(drive: cdrom.OpticalDrive, should_cancel):
        while drive.has_disc():
            if should_cancel():
                raise engine.EngineError("Cancelled")
            time.sleep(1.5)


def main():
    from .uiassets import load_html

    backend = Backend()
    # Frameless: the CRT bezel is the window; the fake Win95 title bar drags
    # (class pywebview-drag-region) and its ✕/minimize go through the bridge.
    # The CRT is authored at BASE_W x BASE_H logical px and scaled down as one
    # unit (CSS zoom via --ui-scale) to fit the monitor's usable work area, so
    # any Windows display-scaling factor works. Below MIN_SCALE the UI would be
    # illegibly small; show a plain message instead.
    wa_x, wa_y, wa_w, wa_h = _work_area()
    scale = min(wa_w / BASE_W, wa_h / BASE_H, 1.0)
    if scale < MIN_SCALE:
        webview.create_window(
            "Audiobook Bob", html=TOO_SMALL_HTML,
            width=560, height=280, resizable=False)
        webview.start()
        return
    backend.ui_scale = round(scale, 4)
    win_w = round(BASE_W * scale)
    win_h = round(BASE_H * scale)
    pos_x = wa_x + max(0, (wa_w - win_w) // 2)
    pos_y = wa_y + max(0, (wa_h - win_h) // 2)
    html = load_html().replace(
        "<body>", f'<body style="--ui-scale:{backend.ui_scale}">', 1)
    window = webview.create_window(
        "Audiobook Bob", html=html, js_api=JsApi(backend),
        width=win_w, height=win_h, x=pos_x, y=pos_y,
        frameless=True, easy_drag=False, resizable=False)
    backend.window = window
    window.events.closing += backend.on_closing
    threading.Thread(target=backend.dispatch_forever, daemon=True).start()
    webview.start()  # positional arg would be treated as a callable


if __name__ == "__main__":
    main()
