"""Orchestration du pipeline, étape par étape, avec sauvegarde de chaque sortie.

    photos ─► transcription (OCR multi-moteurs + arbitrage)
          ─► structure (graphe d'étapes, ancré dans les lignes)
          ─► formalisation (énoncés Lean par étape, fragments filtrés)
          ─► vérification Lean isolée (+ niveau 2 : preuve/réfutation par agent)
          ─► fidélité (ancrage, empreintes, prédicats, sensibilité, rétro-traduction)
          ─► verdict (3 issues) ─► retour en français ─► rapport JSON + HTML

Chaque étape peut être fournie par un fichier (fixture, correction manuelle
d'un enseignant) au lieu d'être calculée : c'est aussi le moyen de rejouer
une exécution sans clé d'API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .lean.sandbox import SandboxConfig
from .lean.verify import verify
from .schemas import (
    Formalization,
    ProofStructure,
    ReferenceStatement,
    RunResult,
    Transcription,
)
from .stages.feedback import build_feedback
from .stages.fidelity import run_fidelity
from .stages.verdict import decide

log = logging.getLogger("mathocr")


@dataclass
class PipelineConfig:
    workspace: Path = Path("lean_workspace")
    sandbox: str = "auto"
    lean_timeout_s: float = 300.0
    lean_memory_mb: int = 8192
    # Moteurs (voir mathocr.llm) ; vides = mode hors-ligne (fixtures obligatoires).
    ocr_engines: list[str] = field(default_factory=list)
    reasoning_engine: str | None = None  # structure, formalisation, niveau 2, arbitrage
    judge_engine: str | None = None  # rétro-traduction (idéalement un autre fournisseur)
    cache_dir: Path = Path("runs/.cache")
    tier2: bool = True
    polish_feedback: bool = False


def _load(path: Path | None, model):
    if path is None:
        return None
    return model(**json.loads(Path(path).read_text(encoding="utf-8")))


def _save(out: Path, name: str, obj) -> None:
    (out / name).write_text(obj.model_dump_json(indent=1), encoding="utf-8")


def run(
    exercise: Path,
    images: list[Path],
    out_dir: Path,
    cfg: PipelineConfig,
    *,
    transcription: Path | None = None,
    structure: Path | None = None,
    formalization: Path | None = None,
) -> RunResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = _load(exercise, ReferenceStatement)

    # 1. Transcription
    tr = _load(transcription, Transcription)
    if tr is None:
        from .stages.transcribe import transcribe
        tr = transcribe(images, ref, cfg)
    else:
        base = Path(transcription).parent
        for p in tr.pages:
            if not Path(p.path).is_absolute() and not Path(p.path).exists():
                cand = (base / p.path) if (base / p.path).exists() else (base.parent / p.path)
                p.path = str(cand)
    _save(out_dir, "1_transcription.json", tr)

    # 2. Structure
    st = _load(structure, ProofStructure)
    if st is None:
        from .stages.agents import extract_structure
        st = extract_structure(ref, tr, cfg)
    _save(out_dir, "2_structure.json", st)

    # 3. Formalisation
    fm = _load(formalization, Formalization)
    if fm is None:
        from .stages.agents import formalize
        fm = formalize(ref, tr, st, cfg)
    _save(out_dir, "3_formalisation.json", fm)

    # 4. Lean
    scfg = SandboxConfig(workspace=cfg.workspace, backend=cfg.sandbox, wall_timeout_s=cfg.lean_timeout_s,
                         memory_mb=cfg.lean_memory_mb)
    lean, evals = verify(ref, st, fm, scfg, out_dir)
    if cfg.tier2 and cfg.reasoning_engine and any(c.status == "non_verifie" for c in lean.steps):
        from .stages.agents import tier2_attempts
        fm = tier2_attempts(ref, st, fm, lean, cfg)
        _save(out_dir, "3b_formalisation_niveau2.json", fm)
        lean, evals = verify(ref, st, fm, scfg, out_dir)
    _save(out_dir, "4_lean.json", lean)

    # 5. Fidélité
    fid = run_fidelity(tr, st, fm, lean, evals)
    if cfg.judge_engine:
        from .stages.agents import backtranslate
        fid += backtranslate(tr, st, fm, cfg)
    (out_dir / "5_fidelite.json").write_text(json.dumps([c.model_dump() for c in fid], ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    _save(out_dir, "1_transcription.json", tr)  # incertitudes enrichies (bloquante / résolution)

    # 6. Verdict et retour
    vr = decide(ref, tr, st, lean, fid, fm)
    fb = build_feedback(tr, st, lean, vr)
    if cfg.polish_feedback and cfg.reasoning_engine:
        from .stages.agents import polish_feedback
        fb = polish_feedback(fb, vr, st, cfg)

    result = RunResult(exercise=ref, transcription=tr, structure=st, formalization=fm, lean=lean,
                       fidelity=fid, verdict=vr, feedback=fb)
    _save(out_dir, "resultat.json", result)
    from .report.html import render_html
    (out_dir / "rapport.html").write_text(render_html(result, out_dir), encoding="utf-8")
    return result
