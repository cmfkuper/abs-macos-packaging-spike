# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Double-click entry point (no console window)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from abscd.webui import main

main()
