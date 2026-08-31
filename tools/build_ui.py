# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Build step: inline abscd/ui/* into abscd/ui_bundle.py for packaging.

Usage:  python tools/build_ui.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abscd.uiassets import build_bundle

out = build_bundle()
print(f"wrote {out} ({out.stat().st_size:,} bytes)")
