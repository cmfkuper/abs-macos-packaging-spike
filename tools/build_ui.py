"""Build step: inline abscd/ui/* into abscd/ui_bundle.py for packaging.

Usage:  python tools/build_ui.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abscd.uiassets import build_bundle

out = build_bundle()
print(f"wrote {out} ({out.stat().st_size:,} bytes)")
