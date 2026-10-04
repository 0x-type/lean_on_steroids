"""Vérifie dans Lean chaque entrée du catalogue des théorèmes citables et écrit son drapeau « verifie ».

Une entrée n'est active que si ses modules s'importent et si chacun de ses lemmes (et compagnons) existe.
Usage : python scripts/verifier_catalogue.py [--lean-workspace lean_workspace]
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAT = ROOT / "src" / "leanonsteroids" / "data" / "theoremes.json"


def check(entry: dict, ws: Path) -> tuple[bool, list[str]]:
    names = entry["lemmes"] + entry.get("compagnons", [])
    src = "\n".join(f"import {m}" for m in ["MathOCRCheck.Prelude", *entry["imports"]]) + "\n\n"
    src += "\n".join(f"#check @{n}" for n in names) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".lean", dir=ws, delete=False) as f:
        f.write(src)
        path = Path(f.name)
    try:
        p = subprocess.run(["lake", "env", "lean", path.name], cwd=ws, capture_output=True, text=True, timeout=900)
    finally:
        path.unlink(missing_ok=True)
    out = p.stdout + p.stderr
    problems = [ln for ln in out.splitlines() if "error" in ln]
    return p.returncode == 0 and not problems, problems[:5]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lean-workspace", default=str(ROOT / "lean_workspace"))
    a = ap.parse_args()
    data = json.loads(CAT.read_text(encoding="utf-8"))
    for e in data["theoremes"]:
        ok, problems = check(e, Path(a.lean_workspace))
        e["verifie"] = ok
        print(f"{'✔' if ok else '✘'} {e['cle']:24s} {'' if ok else ' | '.join(problems)[:300]}", flush=True)
    CAT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
