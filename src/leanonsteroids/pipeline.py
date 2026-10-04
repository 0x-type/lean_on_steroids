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

log = logging.getLogger("leanonsteroids")


@dataclass
class PipelineConfig:
    workspace: Path = Path("lean_workspace")
    sandbox: str = "auto"
    lean_timeout_s: float = 300.0
    lean_memory_mb: int = 8192
    # Moteurs (voir leanonsteroids.llm) ; vides = mode hors-ligne (fixtures obligatoires).
    ocr_engines: list[str] = field(default_factory=list)
    # Mode « cascade » : ocr_engines (bon marché) lisent la page ; seules les lignes disputées
    # sont relues, zoomées, par ocr_strong. audit_rate : part des lignes d'accord relues quand même.
    ocr_mode: str = "ensemble"
    ocr_effort: str = "high"  # effort de raisonnement des lecteurs : low | medium | high
    ocr_strong: list[str] = field(default_factory=list)
    cascade_min_conf: float = 0.75
    audit_rate: float = 0.0
    reasoning_engine: str | None = None  # structure, formalisation, niveau 2, arbitrage
    judge_engine: str | None = None  # rétro-traduction (idéalement un autre fournisseur)
    # Arbitrage des lectures douteuses (regarde l'image) : None = reasoning_engine. Permet de garder un modèle
    # fort sur l'image quand le raisonnement texte passe à un modèle bon marché (Lean contrôle ce dernier).
    arbiter_engine: str | None = None
    # Reprise de niveau 2 et assemblage : Lean contrôle tout, un modèle moins cher suffit. None = reasoning_engine.
    fallback_engine: str | None = None
    assembly_engine: str | None = None
    judge_effort: str = "high"  # effort de raisonnement du juge (rétro-traduction)
    cache_dir: Path = Path("runs/.cache")
    memory_dir: Path | None = Path("runs/.memoire")  # None = pas de mémoire partagée
    photo_gate: bool = True  # refuser localement les photos inutilisables avant tout appel payant
    lazy_judge: bool = True  # arbitre seulement sur les doutes qui peuvent changer le verdict
    tier2: bool = True
    # Niveau 2 (preuve / réfutation d'une étape par un agent) : Lean contrôle chaque preuve proposée,
    # un modèle bon marché ne peut donc pas fausser le verdict. Les étapes qu'il ne tranche pas sont
    # retentées par `reasoning_engine` (au plus `tier2_fallback_max`), car seule une réfutation
    # peut encore changer l'issue.
    tier2_engine: str | None = None  # None = reasoning_engine
    tier2_fallback_max: int = 3
    tier2_parallel: int = 8  # appels de niveau 2 simultanés
    tier2_timeout_s: float | None = 90.0  # au-delà, la tentative est abandonnée
    polish_feedback: bool = False
    # « tolerant » : une étape vraie dont la justification est un calcul élémentaire (démontrée par Lean au
    # niveau 2, sans grand théorème) n'empêche pas « raisonnement vérifié » ; elle est signalée à l'élève.
    # « strict » : toute justification absente est un saut logique.
    tolerance: str = "tolerant"
    # Une étape dont la relecture indépendante juge la traduction infidèle est retraduite une fois.
    fidelity_retry: bool = True


def _load(path: Path | None, model):
    if path is None:
        return None
    return model(**json.loads(Path(path).read_text(encoding="utf-8")))


def _save(out: Path, name: str, obj) -> None:
    (out / name).write_text(obj.model_dump_json(indent=1), encoding="utf-8")


def run(exercise: Path, images: list[Path], out_dir: Path, cfg: "PipelineConfig", **kw) -> RunResult:
    """Exécute le pipeline en comptant les jetons et le coût de chaque appel aux modèles."""
    from .llm.pricing import ledger_scope

    with ledger_scope() as led:
        return _run(exercise, images, out_dir, cfg, led=led, **kw)


def _run(
    exercise: Path,
    images: list[Path],
    out_dir: Path,
    cfg: PipelineConfig,
    *,
    transcription: Path | None = None,
    structure: Path | None = None,
    formalization: Path | None = None,
    led=None,
) -> RunResult:
    from .llm.pricing import set_stage

    out_dir.mkdir(parents=True, exist_ok=True)
    lean_seconds = 0.0
    ref = _load(exercise, ReferenceStatement)
    memory = None
    if cfg.memory_dir is not None:
        from .memory import StepMemory
        memory = StepMemory(cfg.memory_dir, ref.exercise_id)

    # 1. Transcription
    set_stage("transcription")
    tr = _load(transcription, Transcription)
    if tr is None and cfg.photo_gate:
        from .stages.photo_quality import PhotoRejected, check
        reports = [check(p) for p in images]
        if not all(r.ok for r in reports):
            raise PhotoRejected([r for r in reports if not r.ok])
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

    scfg = SandboxConfig(workspace=cfg.workspace, backend=cfg.sandbox, wall_timeout_s=cfg.lean_timeout_s,
                         memory_mb=cfg.lean_memory_mb)

    def downstream(tr: Transcription, st_given: ProofStructure | None = None):
        """Étapes 2 à 5 sans le niveau 2 : structure, formalisation, Lean, fidélité."""
        nonlocal lean_seconds
        set_stage("structure")
        st = st_given or _load(structure, ProofStructure)
        if st is None:
            from .stages.agents import extract_structure
            st = extract_structure(ref, tr, cfg)
        _save(out_dir, "2_structure.json", st)

        set_stage("formalisation")
        fm = _load(formalization, Formalization)
        if fm is None:
            from .stages.agents import formalize
            fm = formalize(ref, tr, st, cfg, memory=memory)
        from .theoremes import citations_ancrees
        cites = citations_ancrees(st, tr)
        for f in fm.steps:
            f.cites = cites.get(f.step_id, [])
        from .theoremes import reperer
        fm.cites_copie = reperer(" ".join(ln.text for ln in tr.lines), approx=True)
        _save(out_dir, "3_formalisation.json", fm)

        set_stage("lean")
        lean, evals = verify(ref, st, fm, scfg, out_dir)
        lean_seconds += lean.run.seconds if lean.run else 0.0
        _save(out_dir, "4_lean.json", lean)

        set_stage("fidélité")
        fid = run_fidelity(tr, st, fm, lean, evals, ref)
        return st, fm, lean, evals, fid

    def tier2(st, fm, lean, evals, only=None):
        """Niveau 2, une seule fois, sur la lecture définitive : modèle bon marché (Lean contrôle tout),
        puis reprise limitée par le modèle de raisonnement des étapes encore non tranchées."""
        nonlocal lean_seconds
        broken = any(c.status == "erreur_formalisation" for c in lean.steps) or bool(lean.policy_violations)
        if not (cfg.tier2 and cfg.reasoning_engine) or broken or not any(
                c.status == "non_verifie" and (only is None or c.step_id in only) for c in lean.steps):
            return fm, lean, evals
        from .stages.agents import tier2_attempts
        set_stage("lean")
        cheap = cfg.tier2_engine or cfg.reasoning_engine
        fm = tier2_attempts(ref, st, fm, lean, cfg, memory=memory, engine=cheap, only=only)
        _save(out_dir, "3b_formalisation_niveau2.json", fm)
        lean, evals = verify(ref, st, fm, scfg, out_dir)
        lean_seconds += lean.run.seconds if lean.run else 0.0
        left = [c.step_id for c in lean.steps
                if c.status == "non_verifie" and (only is None or c.step_id in only)][:cfg.tier2_fallback_max]
        strong = cfg.fallback_engine or cfg.reasoning_engine
        if left and cheap != strong and cfg.tier2_fallback_max > 0:
            fm = tier2_attempts(ref, st, fm, lean, cfg, memory=None, engine=strong, only=set(left))
            _save(out_dir, "3b_formalisation_niveau2.json", fm)
            lean, evals = verify(ref, st, fm, scfg, out_dir)
            lean_seconds += lean.run.seconds if lean.run else 0.0
        _save(out_dir, "4_lean.json", lean)
        return fm, lean, evals

    st, fm, lean, evals, fid = downstream(tr)

    # Arbitre « paresseux » : seulement pour les doutes qui peuvent changer le verdict, et AVANT le
    # niveau 2 (le plus long) : celui-ci ne tourne qu'une fois, sur la lecture définitive.
    if cfg.lazy_judge and cfg.reasoning_engine and transcription is None:
        from .stages.transcribe import adjudicate_needed
        set_stage("arbitrage")
        changed = adjudicate_needed(tr, ref, cfg)
        if changed:
            patched, touches_statements = _patch_structure(st, changed)
            if patched is None:
                st, fm, lean, evals, fid = downstream(tr)  # changement de texte : nouvelle analyse
            elif touches_statements:
                st, fm, lean, evals, fid = downstream(tr, patched)  # retraduction + Lean, sans agent
            else:
                st = patched
                _save(out_dir, "2_structure.json", st)

    def finish(st, fm, lean, evals, only=None, prev_fid=()):
        """Niveau 2, assemblage par agent, fidélité et relecture indépendante.

        La relecture ne dépend que des énoncés traduits (le niveau 2 et l'assemblage n'y touchent pas) : elle
        tourne en parallèle. `only` : après une reprise de traduction, seules ces étapes sont retravaillées ;
        les autres gardent leurs preuves et leur relecture (`prev_fid`)."""
        nonlocal lean_seconds
        judge = None
        if cfg.judge_engine:
            import contextvars
            from concurrent.futures import ThreadPoolExecutor

            from .stages.agents import backtranslate
            fid0 = run_fidelity(tr, st, fm, lean, evals, ref)

            def _judge():
                set_stage("fidélité")
                return backtranslate(tr, st, fm, cfg, fid=fid0, ref=ref, only=only)
            pool = ThreadPoolExecutor(max_workers=1)
            judge = pool.submit(contextvars.copy_context().run, _judge)
            pool.shutdown(wait=False)

        fm, lean, evals = tier2(st, fm, lean, evals, only=only)

        # Raisonnement autre que direct / récurrence (cas, absurde, témoins…) : un agent écrit l'assemblage des
        # étapes de l'élève, que Lean vérifie et que des contrôles empêchent d'ajouter des mathématiques.
        if cfg.reasoning_engine and lean.run is not None and not lean.statement_match and not lean.policy_violations \
                and not any(c.status in ("refute", "erreur_formalisation") for c in lean.steps):
            from .stages.agents import assemble
            set_stage("assemblage")
            fm2, manque = assemble(ref, st, fm, lean, cfg)
            if fm2.assemblage_agent:
                fm = fm2
                _save(out_dir, "3c_assemblage.json", fm)
                set_stage("lean")
                lean, evals = verify(ref, st, fm, scfg, out_dir)
                lean_seconds += lean.run.seconds if lean.run else 0.0
                _save(out_dir, "4_lean.json", lean)
            elif manque:
                lean.assemblage_agent = f"impossible selon l'agent : {manque}"
        set_stage("fidélité")
        fid = run_fidelity(tr, st, fm, lean, evals, ref)

        # Doutes restants : bloquent-ils vraiment le verdict ? Revérification Lean de la lecture alternative.
        if any(u.blocking for u in tr.uncertainties):
            from .stages.fidelity import confirm_alternatives_with_lean
            confirm_alternatives_with_lean(ref, tr, st, fm, lean, fid, scfg)

        if judge is not None:
            fid += judge.result()
            if only is not None:
                fid += [c for c in prev_fid if c.kind == "retrotraduction" and c.step_id not in only]
        return fm, lean, evals, fid

    fm, lean, evals, fid = finish(st, fm, lean, evals)

    # Traduction jugée infidèle par la relecture : une reprise, avec la remarque du relecteur ; la nouvelle
    # traduction repasse par Lean et par la même relecture (rien n'est accepté sans ces contrôles).
    infideles = {c.step_id: c.detail for c in fid if c.kind == "retrotraduction" and not c.ok}
    if infideles and cfg.fidelity_retry and cfg.reasoning_engine and formalization is None:
        from .stages.agents import reformalize
        set_stage("formalisation")
        fm2, changed = reformalize(ref, st, fm, infideles, cfg)
        if changed:
            fm = fm2
            _save(out_dir, "3d_formalisation_reprise.json", fm)
            set_stage("lean")
            lean, evals = verify(ref, st, fm, scfg, out_dir)
            lean_seconds += lean.run.seconds if lean.run else 0.0
            _save(out_dir, "4_lean.json", lean)
            fm, lean, evals, fid = finish(st, fm, lean, evals, only=set(changed), prev_fid=fid)
    (out_dir / "5_fidelite.json").write_text(json.dumps([c.model_dump() for c in fid], ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    _save(out_dir, "1_transcription.json", tr)  # incertitudes enrichies (bloquante / résolution)

    # 6. Verdict et retour
    if memory is not None:
        _remember(memory, st, fm, lean, fid)

    set_stage("retour")
    vr = decide(ref, tr, st, lean, fid, fm, tolerance=cfg.tolerance)
    fb = build_feedback(tr, st, lean, vr)
    if cfg.polish_feedback and cfg.reasoning_engine:
        from .stages.agents import polish_feedback
        fb = polish_feedback(fb, vr, st, cfg)

    result = RunResult(exercise=ref, transcription=tr, structure=st, formalization=fm, lean=lean,
                       fidelity=fid, verdict=vr, feedback=fb,
                       costs=led.report(lean_seconds) if led is not None else None)
    _save(out_dir, "resultat.json", result)
    from .report.html import render_html
    (out_dir / "rapport.html").write_text(render_html(result, out_dir), encoding="utf-8")
    return result


def _patch_structure(st: ProofStructure, changed) -> tuple[ProofStructure | None, bool]:
    """Reporte dans la structure les lectures changées par l'arbitre, sans rappeler l'agent.

    Retourne (structure corrigée, vrai si un énoncé d'étape a changé), ou (None, …) si un changement
    porte sur du texte en toutes lettres dans un énoncé : l'analyse logique est alors refaite."""
    import re

    from .stages.transcribe import _is_letter, _rename_letter

    st = st.model_copy(deep=True)
    touched = False
    for u, old in changed:
        new = u.chosen
        for step in st.steps:
            refs = [r for r in step.source if r.line_id == u.line_id]
            if not refs:
                continue
            for r in refs:
                r.excerpt = (_rename_letter(r.excerpt, old, new) if _is_letter(old) and _is_letter(new)
                             else r.excerpt.replace(old, new, 1))
            if old in step.statement:
                wordy = re.search(r"[A-Za-zÀ-ÿ]{3,}", re.sub(r"\\[A-Za-z]+", "", old + " " + new))
                if wordy:
                    return None, True
                step.statement = (_rename_letter(f"${step.statement}$", old, new)[1:-1]
                                  if _is_letter(old) and _is_letter(new) else step.statement.replace(old, new, 1))
                touched = True
    return st, touched


def _remember(memory, st: ProofStructure, fm: Formalization, lean, fid) -> None:
    """N'enregistre que ce qui a été vérifié : traductions d'agent fidèles, preuves de niveau 2 compilées."""
    from .memory import StepMemory
    from .stages.agents import memory_key

    evidence = {"empreinte_numerique", "application_predicat", "retrotraduction"}
    bad = {sid for c in fid if c.ok is False for sid in c.step_id.split(",")}
    good = {sid for c in fid if c.ok and c.kind in evidence for sid in c.step_id.split(",")}
    status = {c.step_id: c.status for c in lean.steps}
    for f in fm.steps:
        if f.origin == "agent" and f.step_id in good and f.step_id not in bad \
                and status.get(f.step_id) not in ("erreur_formalisation", None):
            memory.put_step(memory_key(st, fm, f.step_id), f, st.step(f.step_id).statement)
        if (f.agent_proof and status.get(f.step_id) in ("verifie_agent", "verifie_theoreme", "verifie_elementaire")) or \
                (f.agent_refutation and status.get(f.step_id) == "refute"):
            binders = " ".join(f"({n} : {t})" for n, t in _binders(st, fm, f.step_id))
            memory.put_tier2(StepMemory.tier2_key(binders, f.claim or ""), f.agent_proof, f.agent_refutation)


def _binders(st, fm, sid):
    from .lean.leangen import LeanGenerator
    from .schemas import ReferenceStatement as _R

    dummy = _R(exercise_id="_", statement_latex="", lean_statement="True")
    return LeanGenerator(dummy, st, fm)._step_binders(sid)
