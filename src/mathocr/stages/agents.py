"""Agents de raisonnement : structure, formalisation, niveau 2, rétro-traduction, reformulation.

Chaque agent a une boucle de réparation limitée aux erreurs *de forme*
(extrait introuvable, énoncé Lean qui ne compile pas, fragment interdit).
Le contenu mathématique n'est jamais « réparé » : ce sont les contrôles de
fidélité et Lean qui jugent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .. import prompts
from ..lean.leangen import LeanGenerator
from ..lean.sandbox import SandboxConfig
from ..lean.verify import verify
from ..llm.base import make_engine
from ..schemas import (
    Binder,
    Feedback,
    FidelityCheck,
    Formalization,
    LeanReport,
    ProofStructure,
    ReferenceStatement,
    ScopeFormal,
    Sides,
    StepFormal,
    Transcription,
    VerdictReport,
)
from .fidelity import check_anchoring

log = logging.getLogger("mathocr.agents")
MAX_REPAIRS = 2


def _engine(cfg, spec: str | None = None):
    spec = spec or cfg.reasoning_engine
    if not spec:
        raise RuntimeError("aucun moteur de raisonnement configuré (--reasoning) ; fournir les fixtures correspondantes")
    return make_engine(spec, Path(getattr(cfg, "cache_dir", "runs/.cache")))


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def extract_structure(ref: ReferenceStatement, tr: Transcription, cfg) -> ProofStructure:
    eng = _engine(cfg)
    lines = "\n".join(f"{ln.id} [{ln.status.value}] {ln.text}" for ln in tr.lines)
    task = prompts.STRUCTURE_TASK.format(statement=ref.statement_latex, lines=lines)
    st = eng.structured(prompts.STRUCTURE_SYSTEM, task, [], ProofStructure)
    for _ in range(MAX_REPAIRS):
        bad = [c for c in check_anchoring(tr, st) if c.ok is False]
        if not bad:
            break
        fix = "\n".join(f"- {c.step_id} : {c.detail}" for c in bad)
        st = eng.structured(prompts.STRUCTURE_SYSTEM,
                            task + "\n\nTa réponse précédente :\n" + st.model_dump_json()
                            + "\n\nProblèmes d'ancrage à corriger (sans rien changer d'autre) :\n" + fix,
                            [], ProofStructure)
    return st


# ---------------------------------------------------------------------------
# Formalisation
# ---------------------------------------------------------------------------


class WireSides(BaseModel):
    relation: Literal["=", "<", "≤", ">", "≥", "≠"]
    lhs_lean: str
    rhs_lean: str
    lhs_latex: str
    rhs_latex: str
    value_type: str = "ℕ"
    variables: list[Binder] = Field(default_factory=list)


class WireStepFormal(BaseModel):
    step_id: str
    role: Literal["def", "prop", "hyp", "none"]
    def_name: str | None = None
    def_binders: list[Binder] = Field(default_factory=list)
    def_body: str | None = None
    claim: str | None = None
    uses: list[str] = Field(default_factory=list)
    sides: WireSides | None = None
    predicate_app: list[str] | None = None
    not_formalized_reason: str | None = None


class WireFormalization(BaseModel):
    scopes: list[ScopeFormal]
    steps: list[WireStepFormal]


def _to_formal(w: WireFormalization, provenance: str) -> Formalization:
    steps = []
    for s in w.steps:
        d = s.model_dump()
        d["sides"] = Sides(**s.sides.model_dump()) if s.sides else None
        d["predicate_app"] = tuple(s.predicate_app[:2]) if s.predicate_app and len(s.predicate_app) >= 2 else None
        steps.append(StepFormal(**d))
    return Formalization(scopes=w.scopes, steps=steps, provenance=provenance)


def _sandbox(cfg) -> SandboxConfig:
    return SandboxConfig(workspace=cfg.workspace, backend=cfg.sandbox, wall_timeout_s=cfg.lean_timeout_s,
                         memory_mb=cfg.lean_memory_mb)


def memory_key(st: ProofStructure, fm: Formalization, sid: str) -> str:
    """Clé de mémoire d'une étape : nature, formule normalisée, variables de portée, définitions."""
    from ..memory import StepMemory

    step = st.step(sid)
    scope_types = {b.name: b.type for sc in fm.scopes for b in sc.binders}
    chain, cur = [], step.scope
    while cur and cur != "global":
        sc = next((x for x in st.scopes if x.id == cur), None)
        if sc is None:
            break
        chain += [(v, scope_types.get(v, "ℕ")) for v in sc.variables]
        cur = sc.parent
    defs = [f"{f.def_name}:{f.def_body}" for f in fm.steps if f.role == "def"]
    return StepMemory.step_key(step.kind, step.statement, chain, defs)


def formalize(ref: ReferenceStatement, tr: Transcription, st: ProofStructure, cfg,
              memory=None) -> Formalization:
    """Code d'abord, puis mémoire partagée, puis agent pour ce qui reste."""
    from .latex2lean import translate_structure

    det = translate_structure(ref, st)
    fm = det.formalization
    if det.failed and memory is not None:
        for sid in list(det.failed):
            hit = memory.get_step(memory_key(st, fm, sid))
            if hit is not None:
                hit.step_id = sid
                hit.uses = [d for d in st.step(sid).depends_on
                            if any(x.id == d and x.kind != "definition" for x in st.steps)]
                fm.steps.append(hit)
                del det.failed[sid]
        order = [s.id for s in st.steps]
        fm.steps.sort(key=lambda f: order.index(f.step_id) if f.step_id in order else len(order))
    if not det.failed:
        return fm
    if not cfg.reasoning_engine:
        # Pas d'agent : les étapes non traduites restent non formalisées (la fidélité le signalera).
        known = {f.step_id for f in fm.steps}
        for sid, why in det.failed.items():
            if sid not in known:
                fm.steps.append(StepFormal(step_id=sid, role="none",
                                           not_formalized_reason=f"traduction automatique impossible : {why}"))
        fm.steps.sort(key=lambda f: [s.id for s in st.steps].index(f.step_id))
        return fm
    llm = _formalize_llm(ref, st, cfg, only=det.failed, done=fm)
    by_id = {f.step_id: f for f in fm.steps}
    for f in llm.steps:
        if f.step_id in det.failed:
            f.origin = "agent"
            by_id[f.step_id] = f
    order = [s.id for s in st.steps]
    steps = sorted(by_id.values(), key=lambda f: order.index(f.step_id) if f.step_id in order else len(order))
    return Formalization(scopes=fm.scopes or llm.scopes, steps=steps,
                         provenance=f"{fm.provenance} ; {len(det.failed)} étape(s) par {llm.provenance}")


def _formalize_llm(ref: ReferenceStatement, st: ProofStructure, cfg, *, only: dict[str, str] | None = None,
                   done: Formalization | None = None) -> Formalization:
    eng = _engine(cfg)
    steps = "\n".join(
        f"- {s.id} [{s.kind}, portée {s.scope}{', implicite ' + s.implicit_reason if s.implicit else ''}] "
        f"« {s.statement} » ; dépend de {s.depends_on or '∅'}"
        f"{' ; justification : ' + s.justification if s.justification else ''}"
        f" ; lignes : " + " | ".join(f"{r.line_id} « {r.excerpt} »" for r in s.source)
        for s in st.steps)
    scopes = "; ".join(f"{sc.id} (variables {sc.variables}, hypothèses {sc.assumptions})" for sc in st.scopes)
    task = prompts.FORMALIZE_TASK.format(statement=ref.statement_latex, lean_statement=ref.lean_statement,
                                         opens=", ".join(ref.lean_opens), steps=steps, scopes=scopes)
    if only and done:
        ctx = "\n".join(f"- {f.step_id} : " + (f"def {f.def_name} := {f.def_body}" if f.role == "def"
                                                 else (f.claim or f"(non formalisée : {f.not_formalized_reason})"))
                         for f in done.steps)
        task += ("\n\nCes étapes sont DÉJÀ formalisées (ne pas les refaire, réutiliser leurs noms) :\n" + ctx
                 + "\n\nFormalise UNIQUEMENT : " + ", ".join(f"{k} ({v})" for k, v in only.items()))
    w = eng.structured(prompts.FORMALIZE_SYSTEM, task, [], WireFormalization)
    fm = _to_formal(w, f"agent {eng.name}")
    merged = lambda f: _merge_partial(f, done, only)  # noqa: E731
    for _ in range(MAX_REPAIRS):
        problems = _formal_problems(ref, st, merged(fm), cfg)
        if not problems:
            break
        w = eng.structured(prompts.FORMALIZE_SYSTEM,
                           task + "\n\nTa réponse précédente :\n" + w.model_dump_json()
                           + "\n\nErreurs de compilation / de politique (corrige la syntaxe Lean, PAS le sens "
                             "mathématique de la copie) :\n" + "\n".join(problems), [], WireFormalization)
        fm = _to_formal(w, f"agent {eng.name} (après réparation syntaxique)")
    return fm


def _merge_partial(llm: Formalization, done: Formalization | None, only: dict | None) -> Formalization:
    if not done or not only:
        return llm
    by_id = {f.step_id: f for f in done.steps}
    for f in llm.steps:
        if f.step_id in only:
            by_id[f.step_id] = f
    return Formalization(scopes=done.scopes or llm.scopes, steps=list(by_id.values()), provenance=llm.provenance)


def _formal_problems(ref, st, fm, cfg) -> list[str]:
    gen = LeanGenerator(ref, st, fm)
    if gen.violations:
        return [f"{v.where} : « {v.token} » interdit ({v.detail})" for v in gen.violations]
    rep, _ = verify(ref, st, fm, _sandbox(cfg))
    out = []
    for c in rep.steps:
        if c.status == "erreur_formalisation":
            out += [f"{c.step_id} : {m.text[:300]}" for m in c.messages[:2]]
    return out


# ---------------------------------------------------------------------------
# Niveau 2 : preuve ou réfutation proposée par un agent pour une étape non vérifiée
# ---------------------------------------------------------------------------


class WireTier2(BaseModel):
    proof: str = ""
    refutation: str = ""
    explanation: str = ""


def tier2_attempts(ref: ReferenceStatement, st: ProofStructure, fm: Formalization, lean: LeanReport,
                   cfg, memory=None, engine: str | None = None, only: set[str] | None = None) -> Formalization:
    from ..memory import StepMemory

    gen = LeanGenerator(ref, st, fm)
    defs = "\n".join(f"def {f.def_name} " + " ".join(f"({b.name} : {b.type})" for b in f.def_binders)
                     + f" : Prop := {f.def_body}" for f in fm.steps if f.role == "def")
    fm = fm.model_copy(deep=True)
    for c in lean.steps:
        if c.status != "non_verifie" or (only is not None and c.step_id not in only):
            continue
        f = fm.of(c.step_id)
        binders = " ".join(f"({n} : {t})" for n, t in gen._step_binders(c.step_id))
        hit = memory.get_tier2(StepMemory.tier2_key(binders, f.claim or "")) if memory is not None else None
        if hit is not None:
            f.agent_proof, f.agent_refutation = hit.get("proof"), hit.get("refutation")
            continue
        eng = _engine(cfg, engine)
        out = eng.structured(prompts.TIER2_SYSTEM,
                             prompts.TIER2_TASK.format(sid=c.step_id, latex=st.step(c.step_id).statement,
                                                       binders=binders, claim=f.claim, defs=defs or "(aucune)"),
                             [], WireTier2)
        f.agent_proof = _admissible(out.proof, f"étape {c.step_id} (preuve agent)")
        f.agent_refutation = _admissible(out.refutation, f"étape {c.step_id} (réfutation agent)")
    return fm


def _admissible(fragment: str, where: str) -> str | None:
    """Une tentative de niveau 2 refusée par la politique est écartée (l'étape reste non vérifiée)
    au lieu d'empêcher Lean de vérifier tout le reste de la copie."""
    from ..lean.policy import check_fragment

    fragment = fragment.strip()
    if not fragment:
        return None
    bad = check_fragment(fragment, where, multiline=True)
    if bad:
        log.warning("niveau 2 écarté (%s) : %s", where, "; ".join(f"{v.token} — {v.detail}" for v in bad))
        return None
    return fragment


# ---------------------------------------------------------------------------
# Rétro-traduction (idéalement par un autre fournisseur que le formaliseur)
# ---------------------------------------------------------------------------


class WireBack(BaseModel):
    step_id: str
    latex: str


class WireBacks(BaseModel):
    items: list[WireBack]


class WireCmp(BaseModel):
    step_id: str
    equivalent: bool
    explanation: str


class WireCmps(BaseModel):
    items: list[WireCmp]


def backtranslate(tr: Transcription, st: ProofStructure, fm: Formalization, cfg,
                  fid: list[FidelityCheck] | None = None) -> list[FidelityCheck]:
    """Relecture indépendante du Lean. Elle sert à détecter un agent infidèle : les étapes traduites
    par le code ET confirmées par l'empreinte numérique ou le prédicat n'en ont pas besoin."""
    evidence = {"empreinte_numerique", "application_predicat"}
    confirmed = {sid for c in (fid or []) if c.ok and c.kind in evidence for sid in c.step_id.split(",")}
    skip = {f.step_id for f in fm.steps if f.origin == "code" and f.step_id in confirmed}
    if all(f.step_id in skip for f in fm.steps if f.role in ("prop", "hyp") and f.claim):
        return []
    eng = _engine(cfg, cfg.judge_engine)
    defs = "\n".join(f"{f.def_name} " + " ".join(f"({b.name} : {b.type})" for b in f.def_binders)
                     + f" := {f.def_body}" for f in fm.steps if f.role == "def") or "(aucune)"
    todo = [f for f in fm.steps if f.role in ("prop", "hyp") and f.claim and f.step_id not in skip]
    claims = "\n".join(f"- {f.step_id} : {f.claim}" for f in todo)
    back = eng.structured(prompts.BACKTRANSLATE_SYSTEM, prompts.BACKTRANSLATE_TASK.format(defs=defs, claims=claims),
                          [], WireBacks)
    bmap = {b.step_id: b.latex for b in back.items}
    pairs = "\n".join(f"- {f.step_id} : copie « {st.step(f.step_id).statement} » / relecture « {bmap.get(f.step_id, '?')} »"
                      for f in todo)
    cmp_ = eng.structured(prompts.COMPARE_SYSTEM, prompts.COMPARE_TASK.format(pairs=pairs), [], WireCmps)
    out = []
    for c in cmp_.items:
        out.append(FidelityCheck(step_id=c.step_id, kind="retrotraduction", ok=c.equivalent,
                                 detail=f"relecture : « {bmap.get(c.step_id, '?')} » — {c.explanation}"))
    return out


# ---------------------------------------------------------------------------
# Reformulation du retour (optionnelle, contrôlée)
# ---------------------------------------------------------------------------


def polish_feedback(fb: Feedback, vr: VerdictReport, st: ProofStructure, cfg) -> Feedback:
    eng = _engine(cfg)
    facts = json.dumps({"issue": vr.verdict.value, "retour": fb.model_dump()}, ensure_ascii=False)
    new = eng.structured(prompts.FEEDBACK_SYSTEM, "Faits à reformuler :\n" + facts, [], Feedback)
    known = {s.id for s in st.steps}
    import re
    cited = set(re.findall(r"\bS\d{2}\b", " ".join([new.summary, *new.points_a_corriger])))
    if not cited <= known or len(new.points_a_corriger) < len(fb.points_a_corriger):
        log.warning("reformulation rejetée (faits ajoutés ou retirés)")
        return fb
    new.generated_by = f"reformulé par {eng.name} à partir du gabarit"
    new.note_pour_correcteur = fb.note_pour_correcteur
    return new
