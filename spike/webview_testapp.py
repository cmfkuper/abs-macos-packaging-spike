"""pywebview packaging spike — proves the webview UI layer freezes into a .app.

Same contract as the tkinter spike (bundled ffmpeg version, /Volumes listing,
PASS/FAIL) plus a button that round-trips JS -> Python -> JS to prove the
bridge survives PyInstaller freezing. Everything is inline HTML/CSS/JS —
no network, no CDN, no external fonts.

Modes:
  (none)        normal window, for a human
  --selftest    no GUI: ffmpeg + imports check, exit code says pass/fail
  --bridgetest  opens the real window, drives the JS bridge automatically,
                destroys the window, exit code says pass/fail (used by CI)
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
import time
from pathlib import Path


def bundled_ffmpeg() -> Path | None:
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    exe_dir = Path(sys.executable).resolve().parent   # .app/Contents/MacOS
    roots += [
        exe_dir,
        exe_dir / "_internal",
        exe_dir.parent / "Frameworks",
        exe_dir.parent / "Resources",
    ]
    for root in roots:
        candidate = root / "ffmpeg"
        if candidate.is_file():
            return candidate
    return None


def gather_report() -> tuple[bool, list[str]]:
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
            lines += banner[:2]
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
    return ok, lines


class Api:
    """Exposed to JavaScript as window.pywebview.api."""

    def ping(self) -> str:
        return (f"pong from Python {sys.version.split()[0]} "
                f"(pid {os.getpid()}, frozen={bool(getattr(sys, 'frozen', False))})")


def build_html(ok: bool, lines: list[str]) -> str:
    report = html.escape("\n".join(lines))
    verdict = "PASS ✅" if ok else "FAIL ❌"
    color = "#2e7d32" if ok else "#c62828"
    # Everything inline: system font stack, no external resources of any kind.
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, "Helvetica Neue", sans-serif;
         margin: 2rem; background: #f5f5f7; color: #1d1d1f; }}
  h1   {{ font-size: 1.4rem; color: {color}; }}
  pre  {{ background: #ffffff; border: 1px solid #d2d2d7; border-radius: 8px;
         padding: 1rem; font-size: 0.85rem; white-space: pre-wrap; }}
  button {{ font-size: 1rem; padding: 0.5rem 1.2rem; border-radius: 8px;
           border: none; background: #0071e3; color: white; cursor: pointer; }}
  #bridge-out {{ margin-top: 0.8rem; font-family: monospace; }}
</style>
</head>
<body>
<h1>pywebview packaging spike: {verdict}</h1>
<pre id="report">{report}</pre>
<button onclick="callPython()">Call Python from JavaScript</button>
<div id="bridge-out">(bridge not called yet)</div>
<script>
function callPython() {{
  document.getElementById('bridge-out').textContent = 'calling…';
  window.pywebview.api.ping().then(function (r) {{
    document.getElementById('bridge-out').textContent = r;
  }}).catch(function (e) {{
    document.getElementById('bridge-out').textContent = 'BRIDGE ERROR: ' + e;
  }});
}}
</script>
</body>
</html>"""


def make_window():
    import webview
    ok, lines = gather_report()
    window = webview.create_window(
        "ABS Webview Spike", html=build_html(ok, lines),
        js_api=Api(), width=680, height=560)
    return webview, window


def run_gui() -> None:
    webview, window = make_window()
    webview.start(window)


def run_bridgetest() -> None:
    """Drive the real window: wait for the bridge, click the button via JS,
    read the result back out of the DOM. Full JS -> Python -> JS round trip."""
    webview, window = make_window()
    outcome = {"bridge": False, "detail": ""}

    def auto(w):
        try:
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    if w.evaluate_js("window.pywebview && window.pywebview.api ? true : false"):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                outcome["detail"] = "pywebview.api never appeared in JS"
                return
            w.evaluate_js("callPython()")
            deadline = time.time() + 15
            while time.time() < deadline:
                out = w.evaluate_js("document.getElementById('bridge-out').textContent")
                if out and "pong from Python" in out:
                    outcome["bridge"] = True
                    outcome["detail"] = out
                    return
                if out and "BRIDGE ERROR" in out:
                    outcome["detail"] = out
                    return
                time.sleep(0.5)
            outcome["detail"] = f"timed out; last DOM text: {out!r}"
        finally:
            w.destroy()

    webview.start(auto, window)
    print("BRIDGE DETAIL:", outcome["detail"])
    print("BRIDGE:", "PASS" if outcome["bridge"] else "FAIL")
    sys.exit(0 if outcome["bridge"] else 1)


def run_selftest() -> None:
    ok, lines = gather_report()
    print("\n".join(lines))
    try:
        import webview  # noqa: F401
        import webview.platforms.cocoa  # noqa: F401  # pulls pyobjc/WebKit
        print("webview + cocoa backend import: OK")
    except Exception as exc:
        print(f"webview import FAILED: {exc!r}")
        ok = False
    sys.exit(0 if ok else 1)


def main() -> None:
    if "--selftest" in sys.argv:
        run_selftest()
    elif "--bridgetest" in sys.argv:
        run_bridgetest()
    else:
        run_gui()


if __name__ == "__main__":
    main()
