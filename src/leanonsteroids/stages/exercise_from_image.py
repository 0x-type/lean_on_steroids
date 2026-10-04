"""Énoncé d'un exercice quelconque, lu sur une capture d'écran.

Un exercice préparé à la main (examples/collecte/exercices) a un énoncé Lean relu une fois. Pour un exercice
quelconque, l'énoncé est formalisé à la volée, puis contrôlé de deux façons avant de servir :
1. Lean compile la proposition (noms, types, modules) ; ses erreurs sont renvoyées au formalisateur (2 reprises) ;
2. le juge (un autre fournisseur, avec l'image) dit si la proposition dit exactement ce que l'énoncé demande.

Seul un énoncé qui passe les deux contrôles est marqué « validé » ; sinon `validated_by` reste vide, et le
verdict « raisonnement vérifié » comme l'erreur « établie » sont alors impossibles (voir stages/verdict.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from .. import prompts
from ..lean.sandbox import SandboxConfig, run_lean
from ..llm.base import make_engine
from ..schemas import Contexte, HypotheseContexte, ObjetContexte, ReferenceStatement

log = logging.getLogger("leanonsteroids.exercice")

MAX_REPAIRS = 2
# Modules déjà compilés dans l'espace Lean (exercices préparés, catalogue des théorèmes) : proposés en priorité.
KNOWN_IMPORTS = [
    "Mathlib.Analysis.Real.Sqrt", "Mathlib.Analysis.SpecificLimits.Basic",
    "Mathlib.Analysis.Calculus.Deriv.MeanValue", "Mathlib.Analysis.Calculus.LocalExtr.Rolle",
    "Mathlib.Topology.Order.IntermediateValue", "Mathlib.Topology.Order.MonotoneConvergence",
    "Mathlib.Topology.Instances.Real.Lemmas", "Mathlib.Topology.UniformSpace.UniformConvergence",
    "Mathlib.Topology.MetricSpace.Sequences", "Mathlib.Algebra.Field.GeomSum",
    "Mathlib.Algebra.EuclideanDomain.Basic", "Mathlib.Data.Nat.Factorization.Defs",
    "Mathlib.NumberTheory.ArithmeticFunction.Misc", "Mathlib.NumberTheory.Wilson",
    "Mathlib.FieldTheory.Finite.Basic", "Mathlib.GroupTheory.Coset.Card",
    "Mathlib.LinearAlgebra.Determinant", "Mathlib.LinearAlgebra.Trace",
]


class WireObjet(BaseModel):
    nom: str
    type: str
    latex: str


class WireHypothese(BaseModel):
    nom: str
    lean: str
    latex: str


class WireExercise(BaseModel):
    titre: str
    statement_latex: str
    objets: list[WireObjet] = Field(default_factory=list)
    hypotheses: list[WireHypothese] = Field(default_factory=list)
    but_lean: str
    lean_imports: list[str] = Field(default_factory=list)
    lean_opens: list[str] = Field(default_factory=list)
    remarques: str = ""


class WireCheck(BaseModel):
    fidele: bool
    explication: str


def build_reference(w: WireExercise, exercise_id: str) -> ReferenceStatement:
    """∀ objets, hypothèses → but : la forme qu'attend le contexte d'exercice (même ordre)."""
    objets = [ObjetContexte(nom=o.nom, type=o.type, latex=o.latex) for o in w.objets]
    hyps = [HypotheseContexte(nom=h.nom if h.nom.startswith("h_") else f"h_{h.nom}", lean=h.lean, latex=h.latex)
            for h in w.hypotheses]
    stmt = w.but_lean.strip()
    if hyps:
        stmt = " → ".join(f"({h.lean})" for h in hyps) + f" → ({stmt})"
    if objets:
        stmt = "∀ " + " ".join(f"({o.nom} : {o.type})" for o in objets) + f", {stmt}"
    imports = ["MathOCRCheck.Prelude"] + [m for m in dict.fromkeys(w.lean_imports) if m != "MathOCRCheck.Prelude"]
    return ReferenceStatement(
        exercise_id=exercise_id, statement_latex=w.statement_latex, notes_latex=w.remarques,
        lean_imports=imports, lean_opens=list(dict.fromkeys(["Finset", *w.lean_opens])), lean_statement=stmt,
        contexte=Contexte(objets=objets, hypotheses=hyps) if objets or hyps else None)


def _missing_imports(imports: list[str], workspace: Path) -> list[str]:
    roots = [workspace / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "lib" / "lean",
             workspace / ".lake" / "build" / "lib" / "lean"]
    out = []
    for m in imports:
        rel = Path(*m.split(".")).with_suffix(".olean")
        if not any((r / rel).exists() for r in roots):
            out.append(m)
    return out


def lean_errors(ref: ReferenceStatement, scfg: SandboxConfig) -> list[str]:
    """Erreurs de compilation de l'énoncé (vide si la proposition est bien formée)."""
    missing = _missing_imports(ref.lean_imports, scfg.workspace)
    if missing:
        return [f"module indisponible : {m} (choisis dans la liste proposée)" for m in missing]
    src = "\n".join(f"import {m}" for m in ref.lean_imports) + "\n\n"
    if ref.lean_opens:
        src += f"open {' '.join(ref.lean_opens)}\n\n"
    src += f"def enonce : Prop :=\n  {ref.lean_statement}\n"
    run = run_lean(src, scfg, filename="Enonce.lean")
    if run.timed_out:
        return ["Lean a dépassé le délai"]
    return [m.text[:400] for m in run.messages if m.severity == "error"][:6]


def prepare_exercise(images: list[Path], cfg, out_dir: Path | None = None) -> ReferenceStatement:
    """Formalise l'énoncé photographié, le fait compiler par Lean et contrôler par le juge."""
    from .transcribe import load_page, to_part

    cache = Path(getattr(cfg, "cache_dir", "runs/.cache"))
    eng = make_engine(cfg.reasoning_engine, cache)
    parts, digest = [], hashlib.sha256()
    for i, p in enumerate(images, start=1):
        _, im = load_page(Path(p), i)
        parts.append(to_part(im, f"énoncé, image {i}"))
        digest.update(Path(p).read_bytes())
    exercise_id = f"auto_{digest.hexdigest()[:10]}"
    scfg = SandboxConfig(workspace=cfg.workspace, backend=cfg.sandbox, wall_timeout_s=cfg.lean_timeout_s,
                         memory_mb=cfg.lean_memory_mb)

    errors: list[str] = []
    w = ref = None
    for _ in range(1 + MAX_REPAIRS):
        extra = ""
        if errors and w is not None:
            extra = ("\n\nTa réponse précédente :\n" + w.model_dump_json() + "\n\nLean la refuse :\n"
                     + "\n".join(errors) + "\nCorrige la syntaxe Lean sans changer le sens de l'énoncé.")
        w = eng.structured(prompts.EXERCISE_SYSTEM,
                           prompts.EXERCISE_TASK.format(imports=", ".join(KNOWN_IMPORTS), erreurs=extra),
                           parts, WireExercise)
        ref = build_reference(w, exercise_id)
        errors = lean_errors(ref, scfg)
        if not errors:
            break
    assert ref is not None and w is not None
    checks = [f"formalisé automatiquement par {eng.name}"]
    if errors:
        checks.append("Lean refuse l'énoncé : " + " | ".join(errors[:2]))
        ref.validated_by = None
    else:
        checks.append("compilé par Lean")
        verdict = None
        if getattr(cfg, "judge_engine", None):
            judge = make_engine(cfg.judge_engine, cache)
            lean_txt = (f"open {' '.join(ref.lean_opens)}\n" if ref.lean_opens else "") \
                + f"theorem enonce : {ref.lean_statement}"
            verdict = judge.structured(prompts.CHECK_EXERCISE_SYSTEM,
                                       prompts.CHECK_EXERCISE_TASK.format(lean=lean_txt,
                                                                          remarques=w.remarques or "(aucune)"),
                                       parts, WireCheck, effort=getattr(cfg, "judge_effort", "high"))
            checks.append(("jugé fidèle à l'énoncé par " if verdict.fidele else "jugé INFIDÈLE par ")
                          + f"{judge.name} : {verdict.explication}")
        # Sans juge, ou juge en désaccord : l'énoncé n'est pas validé (pas de « vérifié » ni d'erreur établie).
        ref.validated_by = " ; ".join(checks) if verdict is not None and verdict.fidele else None
    ref.notes_latex = (w.remarques + "\n" if w.remarques else "") + " ; ".join(checks)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{exercise_id}.json").write_text(
            json.dumps({**ref.model_dump(exclude_none=True), "titre": w.titre}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return ref
