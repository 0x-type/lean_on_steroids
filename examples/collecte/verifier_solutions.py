"""Vérifie que chaque solution de référence de la collecte passe dans le pipeline (traduction par le
code + Lean), étape par étape, avant de la confier à des volontaires.

Usage : python examples/collecte/verifier_solutions.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mathocr.lean.sandbox import SandboxConfig  # noqa: E402
from mathocr.lean.verify import verify  # noqa: E402
from mathocr.schemas import ProofStructure, ReferenceStatement  # noqa: E402
from mathocr.stages.latex2lean import translate_structure  # noqa: E402

PROBLEMES = json.loads((Path(__file__).parent / "problemes.json").read_text())


def structure(p: dict) -> ProofStructure:
    steps = []
    for i, (kind, scope, stmt, deps, *rest) in enumerate(p["etapes"], 1):
        implicit = rest[0] if rest else ""
        steps.append({"id": f"S{i:02d}", "kind": kind, "scope": scope, "statement": stmt,
                      "depends_on": [f"S{d:02d}" for d in deps], "implicit": bool(implicit),
                      "implicit_reason": implicit,
                      "source": [] if implicit else [{"line_id": f"L{i}", "excerpt": stmt}]})
    scopes = [{"id": "global", "parent": None}]
    if p.get("recurrence"):
        hyp = next(s["id"] for s in steps if s["kind"] == "hypothese")
        scopes.append({"id": "H", "parent": "global", "variables": ["n"], "assumptions": [hyp]})
    elif p.get("portee"):
        scopes.append({"id": "H", "parent": "global", "variables": p["portee"]["variables"], "assumptions": []})
    r = p.get("recurrence") or {}
    pattern = ({"kind": "recurrence_simple", "base_step": f"S{r['base']:02d}", "heredity_scope": "H",
                "heredity_step": f"S{r['heredite']:02d}", "conclusion_step": f"S{len(steps):02d}", "variable": "n"}
               if r else {"kind": "direct", "conclusion_step": f"S{len(steps):02d}"})
    return ProofStructure(scopes=scopes, steps=steps, pattern=pattern)


def main() -> int:
    ok_all = True
    for p in PROBLEMES:
        ref = ReferenceStatement(exercise_id=p["id"], statement_latex=p["enonce"], lean_statement=p["lean"],
                                 validated_by="collecte")
        st = structure(p)
        tr = translate_structure(ref, st)
        if tr.failed or tr.incoherent:
            print(f"✘ {p['id']} : traduction impossible {tr.failed or tr.incoherent}")
            ok_all = False
            continue
        rep, _ = verify(ref, st, tr.formalization, SandboxConfig(workspace=ROOT / "lean_workspace"))
        bad = [(c.step_id, c.status) for c in rep.steps
               if c.status not in ("verifie_elementaire", "hypothese", "definition", "non_formalise")]
        ok = not bad and rep.assembly_ok and rep.statement_match and rep.axioms_ok
        ok_all &= ok
        print(f"{'✔' if ok else '✘'} {p['id']:22s} étapes={len(rep.steps)} assemblage={rep.assembly_ok} "
              f"énoncé={rep.statement_match} {bad if bad else ''}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
