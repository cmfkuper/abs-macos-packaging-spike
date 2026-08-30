"""Tkinter front end: three questions, one button, disc-by-disc prompts."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from . import cdrom, engine

POLL_MS = 100

BG = "#1e1f24"
PANEL = "#2a2c33"
FG = "#e8e8ea"
DIM = "#9a9ba3"
ACCENT = "#4f8cff"
GOOD = "#4caf7d"
WARN = "#e0a93e"


class App:
    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self.disc_answer: queue.Queue = queue.Queue()
        self.job: engine.Job | None = None

        self.root = tk.Tk()
        self.root.title("Audiobook CD → M4B")
        self.root.configure(bg=BG)
        self.root.minsize(560, 420)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        base = tkfont.nametofont("TkDefaultFont")
        base.configure(family="Segoe UI", size=10)
        self.h1 = tkfont.Font(family="Segoe UI Semibold", size=15)
        self.mono = tkfont.Font(family="Consolas", size=10)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", background=BG, foreground=DIM)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("PanelDim.TLabel", background=PANEL, foreground=DIM)
        style.configure("TEntry", fieldbackground="#34363f", foreground=FG,
                        insertcolor=FG, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        style.configure("TCombobox", fieldbackground="#34363f", foreground=FG,
                        background="#34363f", arrowcolor=FG)
        style.configure("Big.TButton", font=("Segoe UI Semibold", 11), padding=(18, 8),
                        background=ACCENT, foreground="#ffffff", borderwidth=0)
        style.map("Big.TButton",
                  background=[("active", "#3c74dd"), ("disabled", "#3a3c44")],
                  foreground=[("disabled", "#7a7b83")])
        style.configure("Quiet.TButton", padding=(12, 6), background="#3a3c44",
                        foreground=FG, borderwidth=0)
        style.map("Quiet.TButton", background=[("active", "#484a54")])
        style.configure("Horizontal.TProgressbar", troughcolor="#34363f",
                        background=ACCENT, bordercolor=PANEL,
                        lightcolor=ACCENT, darkcolor=ACCENT)

        self._build_form()
        self._build_progress()
        self._show_form()
        self.root.after(POLL_MS, self._drain_events)

    # ---------- layout ----------

    def _build_form(self):
        f = ttk.Frame(self.root, padding=28)
        self.form = f
        ttk.Label(f, text="Rip an audiobook", font=self.h1).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Answer three questions, insert Disc 1, and press Start.",
                  style="Dim.TLabel").grid(row=1, column=0, columnspan=2,
                                           sticky="w", pady=(2, 18))

        self.var_author = tk.StringVar()
        self.var_title = tk.StringVar()
        self.var_year = tk.StringVar()
        for row, (label, var) in enumerate((
                ("Author", self.var_author),
                ("Book title", self.var_title),
                ("Year published", self.var_year)), start=2):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=6)
            entry = ttk.Entry(f, textvariable=var, width=42, font=("Segoe UI", 11))
            entry.grid(row=row, column=1, sticky="ew", pady=6, padx=(14, 0))
            if row == 2:
                entry.focus_set()

        drives = cdrom.list_optical_drives()
        self.var_drive = tk.StringVar(value=(drives[0] + ":") if drives else "")
        ttk.Label(f, text="CD drive").grid(row=5, column=0, sticky="w", pady=6)
        self.drive_box = ttk.Combobox(
            f, textvariable=self.var_drive, state="readonly", width=6,
            values=[d + ":" for d in drives])
        self.drive_box.grid(row=5, column=1, sticky="w", pady=6, padx=(14, 0))

        self.start_btn = ttk.Button(f, text="Start ripping", style="Big.TButton",
                                    command=self._start)
        self.start_btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        f.columnconfigure(1, weight=1)
        self.root.bind("<Return>", lambda _e: self._start_if_form())

    def _build_progress(self):
        p = ttk.Frame(self.root, padding=28)
        self.progress_frame = p

        self.lbl_book = ttk.Label(p, text="", font=self.h1)
        self.lbl_book.grid(row=0, column=0, sticky="w")
        self.lbl_stage = ttk.Label(p, text="", style="Dim.TLabel")
        self.lbl_stage.grid(row=1, column=0, sticky="w", pady=(2, 16))

        self.bar = ttk.Progressbar(p, mode="determinate", maximum=1000)
        self.bar.grid(row=2, column=0, sticky="ew")
        self.lbl_percent = ttk.Label(p, text="", style="Dim.TLabel", font=self.mono)
        self.lbl_percent.grid(row=3, column=0, sticky="e", pady=(4, 12))

        panel = ttk.Frame(p, style="Panel.TFrame", padding=16)
        panel.grid(row=4, column=0, sticky="nsew", pady=(4, 12))
        panel.columnconfigure(0, weight=1)
        self.lbl_log = ttk.Label(panel, text="", style="Panel.TLabel",
                                 justify="left", anchor="nw", font=self.mono)
        self.lbl_log.grid(row=0, column=0, sticky="nsew")
        self.log_lines: list[str] = []

        btns = ttk.Frame(p)
        btns.grid(row=5, column=0, sticky="ew")
        btns.columnconfigure(0, weight=1)
        self.next_btn = ttk.Button(btns, text="Rip next disc", style="Big.TButton",
                                   command=lambda: self._answer("next"))
        self.done_btn = ttk.Button(btns, text="That was the last disc — make the M4B",
                                   style="Big.TButton", command=lambda: self._answer("done"))
        self.cancel_btn = ttk.Button(btns, text="Cancel", style="Quiet.TButton",
                                     command=self._cancel)
        self.cancel_btn.grid(row=0, column=2, sticky="e")
        self.new_btn = ttk.Button(btns, text="Rip another book", style="Big.TButton",
                                  command=self._back_to_form)

        p.columnconfigure(0, weight=1)
        p.rowconfigure(4, weight=1)

    # ---------- view switching ----------

    def _show_form(self):
        self.progress_frame.pack_forget()
        self.form.pack(fill="both", expand=True)

    def _show_progress(self):
        self.form.pack_forget()
        self.progress_frame.pack(fill="both", expand=True)
        self._hide_disc_buttons()
        self.new_btn.grid_forget()
        self.cancel_btn.grid(row=0, column=2, sticky="e")

    def _hide_disc_buttons(self):
        self.next_btn.grid_forget()
        self.done_btn.grid_forget()

    def _show_disc_buttons(self):
        self.next_btn.grid(row=0, column=0, sticky="w")
        self.done_btn.grid(row=0, column=1, sticky="w", padx=(10, 0))

    # ---------- actions ----------

    def _start_if_form(self):
        if self.form.winfo_ismapped():
            self._start()

    def _start(self):
        author = self.var_author.get().strip()
        title = self.var_title.get().strip()
        year = self.var_year.get().strip()
        drive = self.var_drive.get().strip().rstrip(":")
        if not author or not title or not year:
            messagebox.showwarning("Missing answer",
                                   "Please fill in the author, book title, and year.")
            return
        if not drive:
            messagebox.showwarning("No CD drive",
                                   "No optical drive was found on this computer.")
            return
        if not engine.locate_ffmpeg():
            messagebox.showerror(
                "ffmpeg missing",
                "ffmpeg is required to build the M4B but was not found.\n\n"
                "Install it with:  winget install Gyan.FFmpeg\nthen restart this program.")
            return

        self.job = engine.Job.create(self.output_root, author, title, year)
        self.cancel_flag.clear()
        self.log_lines.clear()
        self.lbl_log.configure(text="")
        self.lbl_book.configure(text=f"{title} — {author} ({year})")
        self._show_progress()

        self.worker = threading.Thread(
            target=self._run_job, args=(cdrom.OpticalDrive(drive),), daemon=True)
        self.worker.start()

    def _answer(self, value: str):
        self._hide_disc_buttons()
        self.disc_answer.put(value)

    def _cancel(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Cancel", "Stop ripping this audiobook?"):
                return
            self.cancel_flag.set()
            self.disc_answer.put("cancel")
        else:
            self._back_to_form()

    def _back_to_form(self):
        self.var_author.set("")
        self.var_title.set("")
        self.var_year.set("")
        self._show_form()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "A rip is in progress. Quit anyway?"):
                return
            self.cancel_flag.set()
            self.disc_answer.put("cancel")
        self.root.destroy()

    # ---------- worker thread ----------

    def _emit(self, kind: str, **payload):
        self.events.put((kind, payload))

    def _run_job(self, drive: cdrom.OpticalDrive):
        job = self.job
        cancelled = self.cancel_flag.is_set
        try:
            ripper = engine.Ripper(job, drive)
            resumed = sorted(d.number for d in job.discs)
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
                    engine_wait_for_removal(drive, cancelled)
                    continue

                minutes = toc.seconds / 60
                self._emit("stage",
                           text=f"{job.base_name}, Disc {disc_number} — "
                                f"{len(toc.audio_tracks)} tracks, {minutes:.0f} min")
                record = ripper.rip_disc(
                    toc, disc_number,
                    progress=lambda d, t: self._emit("progress", done=d, total=t),
                    status=lambda s: self._emit("stage", text=s),
                    should_cancel=cancelled,
                )
                note = f" ({ripper.bad_sectors} unreadable sectors patched)" \
                    if ripper.bad_sectors else ""
                self._emit("log", text=f"✔ Disc {record.number} ripped — "
                                       f"{record.tracks} tracks{note}")
                drive.eject()
                disc_number += 1

                self._emit("ask_disc", next_number=disc_number)
                answer = self.disc_answer.get()
                if answer == "cancel":
                    raise engine.EngineError("Cancelled")
                if answer == "done":
                    break

            encoder = engine.Encoder(job, bitrate="96k", channels=2)
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
            self._emit("finished", path=str(out))
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

    # ---------- UI thread event pump ----------

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "stage":
                    self.lbl_stage.configure(text=payload["text"])
                elif kind == "progress":
                    done, total = payload["done"], max(payload["total"], 1)
                    self.bar.configure(value=1000 * done / total)
                    self.lbl_percent.configure(
                        text=f"{100 * done / total:5.1f} %" if payload["total"] > 1 else "")
                elif kind == "log":
                    self.log_lines.append(payload["text"])
                    self.lbl_log.configure(text="\n".join(self.log_lines[-12:]))
                elif kind == "ask_disc":
                    n = payload["next_number"]
                    self.lbl_stage.configure(
                        text=f"Disc ejected. Insert Disc {n}, or finish the book.")
                    self.bar.configure(value=0)
                    self.lbl_percent.configure(text="")
                    self.next_btn.configure(text=f"Rip Disc {n}")
                    self._show_disc_buttons()
                elif kind == "finished":
                    self.lbl_stage.configure(text="Done! Your audiobook is ready.")
                    self.bar.configure(value=1000)
                    self.lbl_percent.configure(text="100.0 %")
                    self._hide_disc_buttons()
                    self.cancel_btn.grid_forget()
                    self.new_btn.grid(row=0, column=0, sticky="w")
                    messagebox.showinfo(
                        "Audiobook ready",
                        f"Saved:\n{payload['path']}\n\nCopy this file into your "
                        "Audiobookshelf library.")
                elif kind == "aborted":
                    self.lbl_stage.configure(text=payload["text"])
                    self._hide_disc_buttons()
                    self.cancel_btn.grid_forget()
                    self.new_btn.grid(row=0, column=0, sticky="w")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain_events)

    def run(self):
        self.root.mainloop()


def engine_wait_for_removal(drive: cdrom.OpticalDrive, should_cancel):
    """Wait until the duplicate disc is taken out before polling for the next one."""
    import time
    while drive.has_disc():
        if should_cancel():
            raise engine.EngineError("Cancelled")
        time.sleep(1.5)


def main():
    project_root = Path(__file__).resolve().parent.parent
    App(output_root=project_root / "Output").run()


if __name__ == "__main__":
    main()
