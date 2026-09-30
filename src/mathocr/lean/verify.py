"""Vérification Lean : génération, exécution isolée, interprétation par étape."""

from __future__ import annotations

import re
from pathlib import Path

from ..schemas import (
    Formalization,
    LeanMessage,
    LeanReport,
    ProofStructure,
    ReferenceStatement,
    StepCheck,
)
from .leangen import GeneratedLean, LeanGenerator, Segment
from .policy import check_axioms
from .sandbox import SandboxConfig, run_lean

_AXIOMS_RE = re.compile(r"'([^']+)' depends on axioms: \[([^\]]*)\]")
_NO_AXIOMS_RE = re.compile(r"'([^']+)' does not depend on any axioms")


def _in(seg: Segment, m: LeanMessage) -> bool:
    return seg.start <= m.line <= seg.end


def _errors(seg: Segment | None, msgs: list[LeanMessage]) -> list[LeanMessage]:
    if seg is None:
        return []
    return [m for m in msgs if _in(seg, m) and m.severity == "error"]


def _trace(seg: Segment, msgs: list[LeanMessage], prefix: str) -> str | None:
    for m in msgs:
        if _in(seg, m) and m.text.startswith(prefix):
            return m.text[len(prefix):].strip()
    return None


def interpret(
    gen: GeneratedLean,
    msgs: list[LeanMessage],
    structure: ProofStructure,
    formal: Formalization,
    source_lines: list[str],
) -> tuple[list[StepCheck], dict[str, list[str]], bool, bool, dict]:
    by_kind: dict[tuple[str, str | None], Segment] = {}
    for s in gen.segments:
        if s.kind not in ("eval", "axioms"):
            by_kind[(s.kind, s.step_id)] = s

    # Axiomes
    axioms: dict[str, list[str]] = {}
    for m in msgs:
        if m.severity != "information":
            continue
        a = _AXIOMS_RE.search(m.text)
        if a:
            axioms[a.group(1)] = [x.strip() for x in a.group(2).split(",") if x.strip()]
            continue
        b = _NO_AXIOMS_RE.search(m.text)
        if b:
            axioms[b.group(1)] = []

    # Rattacher les messages aux étapes
    for m in msgs:
        for s in gen.segments:
            if _in(s, m) and s.step_id:
                m.step_id = s.step_id
                break

    checks: list[StepCheck] = []
    for step in structure.steps:
        try:
            f = formal.of(step.id)
        except KeyError:
            checks.append(StepCheck(step_id=step.id, decl="", status="non_formalise",
                                    closed_by="aucune formalisation fournie"))
            continue
        if f.role == "none":
            checks.append(StepCheck(step_id=step.id, decl="", status="non_formalise",
                                    closed_by=f.not_formalized_reason))
            continue
        if f.role == "hyp":
            checks.append(StepCheck(step_id=step.id, decl=f"h_{step.id}", status="hypothese"))
            continue
        if f.role == "def":
            seg = by_kind.get(("def", step.id))
            errs = _errors(seg, msgs)
            checks.append(StepCheck(
                step_id=step.id, decl=f"Copie.{f.def_name}",
                status="erreur_formalisation" if errs else "definition",
                messages=errs, lean_snippet=_snippet(seg, source_lines),
                lean_lines=(seg.start, seg.end) if seg else None))
            continue

        seg = by_kind.get(("step", step.id))
        errs = _errors(seg, msgs)
        decl = f"Copie.{step.id}"
        ax = axioms.get(decl, ["?"])
        statement_errors = [e for e in errs if seg and e.line <= seg.start + 2 and _is_statement_error(e)]
        check = StepCheck(step_id=step.id, decl=decl, status="non_verifie", messages=errs,
                          lean_snippet=_snippet(seg, source_lines),
                          lean_lines=(seg.start, seg.end) if seg else None)
        if statement_errors:
            check.status = "erreur_formalisation"
        elif not errs and "sorryAx" not in ax:
            check.status = "verifie_elementaire"
            check.closed_by = _trace(seg, msgs, "mathocr:closed_by=") if seg else None
            if step.implicit_reason == "schema_recurrence" and not check.closed_by:
                check.closed_by = "schéma de récurrence (gabarit)"
        else:
            agent = by_kind.get(("agent_proof", step.id))
            if agent and not _errors(agent, msgs) and "sorryAx" not in axioms.get(f"Copie.{step.id}_agent", ["?"]):
                check.status = "verifie_agent"
                check.closed_by = "preuve proposée par un agent (niveau 2)"
        # Réfutation (automatique ou proposée par un agent)
        for kind in ("refutation", "agent_refutation"):
            rs = by_kind.get((kind, step.id))
            if rs and not _errors(rs, msgs):
                check.status = "refute"
                check.counterexample = _trace(rs, msgs, "mathocr:cex=") or "réfutation proposée par un agent"
                break
        sd = by_kind.get(("sans_dep", step.id))
        if sd is not None:
            check.independent_of_deps = not _errors(sd, msgs)
        checks.append(check)

    assembly_seg = by_kind.get(("assembly", structure.pattern.conclusion_step))
    accord_seg = by_kind.get(("accord", None))
    assembly_ok = assembly_seg is not None and not _errors(assembly_seg, msgs) and \
        "sorryAx" not in axioms.get("Copie.assemblage", ["sorryAx"])
    statement_match = accord_seg is not None and not _errors(accord_seg, msgs) and \
        "sorryAx" not in axioms.get("Copie.accord_enonce", ["sorryAx"])

    # Empreintes numériques côté Lean : {step: [(point, lhs, rhs)]}
    evals: dict[str, dict] = {}
    for s in gen.segments:
        if s.kind != "eval":
            continue
        vals = [m for m in msgs if _in(s, m)]
        val = None
        for m in vals:
            if m.severity == "information":
                val = m.text.strip()
            elif m.severity == "error":
                val = None
                break
        key = tuple(sorted(s.meta["point"].items()))
        evals.setdefault(s.step_id, {}).setdefault(key, {})[s.meta["side"]] = val
    return checks, axioms, assembly_ok, statement_match, evals


def _is_statement_error(m: LeanMessage) -> bool:
    t = m.text
    return any(k in t for k in ("unknown identifier", "unknown constant", "type mismatch", "failed to synthesize",
                                "unexpected token", "expected", "function expected", "elaboration"))


def _snippet(seg: Segment | None, lines: list[str]) -> str:
    if seg is None:
        return ""
    return "\n".join(lines[seg.start - 1: seg.end]).rstrip()


def verify(
    ref: ReferenceStatement,
    structure: ProofStructure,
    formal: Formalization,
    cfg: SandboxConfig,
    out_dir: Path | None = None,
) -> tuple[LeanReport, dict]:
    gen = LeanGenerator(ref, structure, formal).generate()
    file_path = ""
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "Copie.lean").write_text(gen.source, encoding="utf-8")
        file_path = str(out_dir / "Copie.lean")
    report = LeanReport(file=file_path, policy_violations=gen.violations)
    if gen.violations:
        # Un fragment interdit : on ne compile même pas.
        return report, {}
    run = run_lean(gen.source, cfg)
    report.run = run
    lines = gen.source.splitlines()
    checks, axioms, assembly_ok, statement_match, evals = interpret(gen, run.messages, structure, formal, lines)
    report.steps = checks
    report.axioms = axioms
    report.assembly_ok = assembly_ok and not run.timed_out
    report.statement_match = statement_match and not run.timed_out
    final = {k: axioms[k] for k in ("Copie.assemblage", "Copie.accord_enonce") if k in axioms}
    report.axioms_ok = len(final) == 2 and not check_axioms(final)
    return report, evals
