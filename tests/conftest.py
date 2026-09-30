import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples" / "somme_impairs"


def load(name, sub="fixtures"):
    return json.loads((EX / sub / name).read_text())


def lean_available() -> bool:
    ws = ROOT / "lean_workspace" / ".lake" / "build" / "lib" / "lean" / "MathOCRCheck" / "Auto.olean"
    return ws.exists() and (shutil.which("lake") or (Path.home() / ".elan/bin/lake").exists())


needs_lean = pytest.mark.skipif(not lean_available(), reason="Lean/Mathlib non compilés (voir README)")
