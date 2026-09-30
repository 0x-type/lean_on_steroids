"""Contrôle de fidélité : la formalisation dit-elle ce que la copie dit ?

Une preuve Lean valide ne vaut rien si elle ne porte pas sur ce que l'élève a
écrit. On contrôle donc, sans faire confiance aux agents :

* **ancrage** — chaque extrait cité par une étape existe dans la transcription ;
* **structure** — pas d'étape Lean sans étape écrite, pas de dépendance ajoutée,
  étapes implicites limitées à une liste fermée de motifs ;
* **couverture** — toute ligne de calcul de la copie est rattachée à une étape ;
* **empreinte numérique** — les membres Lean et les membres LaTeX de la copie
  prennent les mêmes valeurs aux points d'échantillonnage ;
* **application de prédicat** — « P(n+1) est vraie » ↔ `P (n + 1)` ;
* **sensibilité aux lectures** — une lecture alternative plausible d'un passage
  ambigu changerait-elle une étape formalisée ?
* **rétro-traduction** (optionnelle, agent) — un agent relit le Lean sans voir la copie.
"""

from __future__ import annotations

import re

from ..schemas import (
    FidelityCheck,
    Formalization,
    LeanReport,
    ProofStructure,
    StepFormal,
    Transcription,
    Uncertainty,
)
from .latexeval import LatexEvalError, evaluate, free_symbols, parse_lean_value

LOW_PROBABILITY = 0.15
IMPLICIT_ALLOWED = {"chaine_egalites", "depliage_definition", "schema_recurrence"}

# Formulations françaises équivalentes pour une même affirmation formelle.
EQUIVALENT_PHRASES = [
    {"est nulle", "est vide", "vaut 0", "vaut zéro", "est égale à 0", "= 0", "est nul"},
    {"est vraie", "est vérifiée", "est vrai", "vraie", "vérifiée", "est satisfaite"},
    {"donc", "ainsi", "d'où", "par conséquent", "alors"},
]


def norm(s: str) -> str:
    s = s.replace("$", "")
    for a in (r"\left", r"\right", r"\,", r"\;", r"\!", r"\displaystyle", "\\ "):
        s = s.replace(a, "")
    return re.sub(r"\s+", "", s)


def _math_segments(text: str) -> list[str]:
    return re.findall(r"\$([^$]+)\$", text)


def _pred_apps_latex(text: str) -> set[tuple[str, str]]:
    return {(m.group(1), norm(m.group(2))) for m in re.finditer(r"(?<![A-Za-z\\])([A-Z])\(([^()]*)\)", text)}


def _pred_apps_lean(text: str, names: set[str]) -> set[tuple[str, str]]:
    out = set()
    for m in re.finditer(r"(?<![A-Za-z_.])([A-Z][A-Za-z0-9_']*)\s+(\([^()]*\)|[A-Za-z0-9_']+)", text):
        if m.group(1) in names:
            arg = m.group(2)
            if arg.startswith("(") and arg.endswith(")"):
                arg = arg[1:-1]
            out.add((m.group(1), norm(arg)))
    return out


def _step_lines_text(tr: Transcription, line_ids: list[str]) -> str:
    out = []
    for lid in line_ids:
        try:
            out.append(tr.line(lid).text)
        except KeyError:
            pass
    return " ".join(out)


# ---------------------------------------------------------------------------


def check_anchoring(tr: Transcription, st: ProofStructure) -> list[FidelityCheck]:
    out = []
    for s in st.steps:
        if not s.source and not s.implicit:
            out.append(FidelityCheck(step_id=s.id, kind="ancrage", ok=False,
                                     detail="étape sans passage de la copie associé"))
            continue
        bad = []
        for ref in s.source:
            try:
                line = tr.line(ref.line_id)
            except KeyError:
                bad.append(f"ligne inconnue {ref.line_id}")
                continue
            if ref.excerpt and norm(ref.excerpt) not in norm(line.text):
                bad.append(f"extrait « {ref.excerpt} » absent de {ref.line_id}")
            if not ref.excerpt and not s.implicit:
                bad.append(f"extrait vide pour {ref.line_id}")
        if s.implicit and s.implicit_reason not in IMPLICIT_ALLOWED:
            bad.append(f"étape implicite non autorisée ({s.implicit_reason or 'motif absent'})")
        out.append(FidelityCheck(step_id=s.id, kind="ancrage", ok=not bad,
                                 detail="; ".join(bad) if bad else
                                 ("étape implicite : " + s.implicit_reason if s.implicit else
                                  "extraits retrouvés dans la transcription")))
    return out


def check_structure(st: ProofStructure, fm: Formalization) -> list[FidelityCheck]:
    out = []
    ids = {s.id for s in st.steps}
    for f in fm.steps:
        if f.step_id not in ids:
            out.append(FidelityCheck(step_id=f.step_id, kind="structure", ok=False,
                                     detail="étape formalisée sans étape écrite correspondante"))
            continue
        step = st.step(f.step_id)
        extra = [u for u in f.uses if u not in step.depends_on]
        if extra:
            out.append(FidelityCheck(step_id=f.step_id, kind="structure", ok=False,
                                     detail=f"la formalisation ajoute des dépendances non présentes dans la copie : {extra}"))
        if f.role == "hyp" and step.kind != "hypothese":
            out.append(FidelityCheck(step_id=f.step_id, kind="structure", ok=False,
                                     detail="une affirmation de l'élève est traitée comme une hypothèse (elle ne serait pas vérifiée)"))
        if f.role == "none" and step.kind in ("affirmation", "calcul", "conclusion"):
            out.append(FidelityCheck(step_id=f.step_id, kind="structure", ok=False,
                                     detail=f"affirmation non formalisée : {f.not_formalized_reason or 'sans raison'}"))
    formal_ids = {f.step_id for f in fm.steps}
    for s in st.steps:
        if s.id not in formal_ids and s.kind in ("affirmation", "calcul", "conclusion", "hypothese", "definition"):
            out.append(FidelityCheck(step_id=s.id, kind="structure", ok=False, detail="étape écrite absente de la formalisation"))
    return out


def check_coverage(tr: Transcription, st: ProofStructure) -> list[FidelityCheck]:
    used = {r.line_id for s in st.steps for r in s.source}
    out = []
    for ln in tr.lines:
        if ln.status.value != "normal" or ln.id in used:
            continue
        has_math = "$" in ln.text
        declared = ln.id in st.unmapped_lines
        ok = not has_math
        detail = ("ligne de calcul non rattachée à une étape : une étape de la copie pourrait manquer"
                  if has_math else
                  ("ligne sans contenu mathématique (titre, transition)" + ("" if declared else ", non déclarée")))
        out.append(FidelityCheck(step_id=ln.id, kind="couverture", ok=ok, detail=detail))
    return out


def _sides_in_copy(f: StepFormal, st: ProofStructure, tr: Transcription) -> tuple[bool, str]:
    """Les membres LaTeX déclarés figurent-ils dans la copie (lignes de l'étape ou de l'étape précédente d'une chaîne) ?"""
    step = st.step(f.step_id)
    line_ids = [r.line_id for r in step.source]
    idx = [s.id for s in st.steps].index(step.id)
    if step.kind == "calcul" and idx > 0:
        line_ids += [r.line_id for r in st.steps[idx - 1].source]
    if step.kind == "conclusion" or step.kind == "affirmation":
        pass
    text = norm(_step_lines_text(tr, line_ids))
    missing = [x for x in (f.sides.lhs_latex, f.sides.rhs_latex) if norm(x) not in text]
    if step.kind == "definition":
        text2 = norm(step.statement)
        missing = [x for x in missing if norm(x) not in text2]
    if missing:
        return False, f"membres absents de la copie : {missing}"
    return True, "membres présents dans la copie"


def check_fingerprints(fm: Formalization, st: ProofStructure, tr: Transcription,
                       lean_evals: dict) -> list[FidelityCheck]:
    out = []
    for f in fm.steps:
        if not f.sides:
            continue
        present, why = _sides_in_copy(f, st, tr)
        if not present:
            out.append(FidelityCheck(step_id=f.step_id, kind="empreinte_numerique", ok=False, detail=why))
            continue
        claim_n = norm(f.claim or f.def_body or "")
        if norm(f.sides.lhs_lean) not in claim_n or norm(f.sides.rhs_lean) not in claim_n:
            out.append(FidelityCheck(step_id=f.step_id, kind="empreinte_numerique", ok=False,
                                     detail="les membres Lean déclarés ne figurent pas dans l'énoncé Lean de l'étape"))
            continue
        points = lean_evals.get(f.step_id)
        if not points:
            out.append(FidelityCheck(step_id=f.step_id, kind="empreinte_numerique", ok=None,
                                     detail="pas d'évaluation Lean disponible (type non calculable ?)"))
            continue
        mismatches, compared, unevaluable = [], 0, []
        for key, sides in points.items():
            pt = dict(key)
            for side in ("lhs", "rhs"):
                lean_v = parse_lean_value(sides.get(side))
                latex = f.sides.lhs_latex if side == "lhs" else f.sides.rhs_latex
                cv = evaluate(latex, pt)
                if lean_v is None or cv.value is None:
                    unevaluable.append(f"{side}@{pt}: {cv.error or 'Lean non évalué'}")
                    continue
                compared += 1
                if cv.value != lean_v:
                    mismatches.append(f"{side} en {pt} : copie {cv.value} ≠ Lean {lean_v}")
        if mismatches:
            out.append(FidelityCheck(step_id=f.step_id, kind="empreinte_numerique", ok=False,
                                     detail="; ".join(mismatches[:4])))
        elif compared == 0:
            out.append(FidelityCheck(step_id=f.step_id, kind="empreinte_numerique", ok=None,
                                     detail="aucune comparaison possible : " + "; ".join(unevaluable[:2])))
        else:
            out.append(FidelityCheck(step_id=f.step_id, kind="empreinte_numerique", ok=True,
                                     detail=f"{compared} valeurs identiques (copie vs Lean)"))
    return out


def check_predicates(fm: Formalization, st: ProofStructure) -> list[FidelityCheck]:
    names = {f.def_name for f in fm.steps if f.role == "def" and f.def_name}
    out = []
    if not names:
        return out
    for f in fm.steps:
        if f.role not in ("prop", "hyp") or not f.claim or f.sides:
            continue
        step = st.step(f.step_id)
        if step.implicit:
            continue
        excerpt = " ".join(r.excerpt for r in step.source)
        lat = {p for p in _pred_apps_latex(excerpt) if p[0] in names}
        lean = _pred_apps_lean(f.claim, names)
        if not lat and not lean:
            continue
        ok = lat == lean
        out.append(FidelityCheck(
            step_id=f.step_id, kind="application_predicat", ok=ok,
            detail=(f"mêmes applications : {sorted(lat)}" if ok else
                    f"copie {sorted(lat)} ≠ Lean {sorted(lean)}")))
    return out


# ---------------------------------------------------------------------------
# Sensibilité aux lectures ambiguës
# ---------------------------------------------------------------------------


def _substitute(text: str, u: Uncertainty, alt: str) -> str:
    if u.replace_all and re.fullmatch(r"[A-Za-z]", u.chosen):
        return re.sub(rf"(?<![\\A-Za-z]){re.escape(u.chosen)}(?![A-Za-z])", alt, text)
    if u.replace_all:
        return text.replace(u.chosen, alt)
    return text.replace(u.chosen, alt, 1)


def _phrase_equivalent(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or any(a in grp and b in grp for grp in EQUIVALENT_PHRASES)


def analyse_uncertainties(tr: Transcription, st: ProofStructure, fm: Formalization,
                          lean_evals: dict) -> list[FidelityCheck]:
    out = []
    defs = {f.def_name for f in fm.steps if f.role == "def" and f.def_name}
    for u in tr.uncertainties:
        affected = [s for s in st.steps
                    if any(r.line_id == u.line_id and (u.chosen in r.excerpt or norm(u.chosen) in norm(r.excerpt))
                           for r in s.source)]
        formal_affected = []
        for s in affected:
            try:
                f = fm.of(s.id)
            except KeyError:
                continue
            if f.role != "none":
                formal_affected.append((s, f))
        alts = [r for r in u.readings if r.text != u.chosen]
        verdicts: list[tuple[bool, str]] = []  # (bloquant, raison)
        if not formal_affected:
            verdicts.append((False, "n'affecte aucune étape formalisée"))
        for alt in alts:
            in_math = any(u.chosen in seg for seg in _math_segments(tr.line(u.line_id).text)) or "\\" in u.chosen
            if not in_math:
                if _phrase_equivalent(u.chosen, alt.text):
                    verdicts.append((False, f"« {alt.text} » : même sens mathématique"))
                elif alt.score < LOW_PROBABILITY and not u.context_dependent:
                    verdicts.append((False, f"« {alt.text} » : lecture peu probable ({alt.score:.2f})"))
                else:
                    verdicts.append((bool(formal_affected), f"« {alt.text} » : changerait le texte de l'étape"))
                continue
            # Lecture mathématique : l'alternative introduit-elle un symbole non défini ?
            allowed = set(defs)
            for _, f in formal_affected:
                if f.sides:
                    allowed |= {b.name for b in f.sides.variables}
            for sc in fm.scopes:
                allowed |= {b.name for b in sc.binders}
            try:
                syms = free_symbols(alt.text)
                syms_chosen = free_symbols(u.chosen)
            except LatexEvalError:
                syms, syms_chosen = None, None
            renaming = u.replace_all and re.fullmatch(r"[A-Za-z]", u.chosen) is not None
            if syms is not None and not renaming:
                unknown = syms - allowed - syms_chosen
                pred = re.fullmatch(r"\s*([A-Z])\((.*)\)\s*", alt.text)
                if pred and pred.group(1) not in defs:
                    unknown |= {pred.group(1)}
                if unknown:
                    verdicts.append((False, f"« {alt.text} » : lecture mal formée (symbole non défini : {', '.join(sorted(unknown))})"))
                    continue
            # Bien formée : change-t-elle la valeur d'une étape formalisée ?
            changed, compared = False, False
            for s, f in formal_affected:
                if not f.sides:
                    continue
                for latex in (f.sides.lhs_latex, f.sides.rhs_latex):
                    if u.chosen not in latex and not u.replace_all:
                        continue
                    alt_latex = _substitute(latex, u, alt.text)
                    if alt_latex == latex:
                        continue
                    pts = [dict(k) for k in (lean_evals.get(s.id) or {(): {}}).keys()]
                    for pt in pts:
                        a, b = evaluate(latex, pt), evaluate(alt_latex, pt)
                        if a.value is None or b.value is None:
                            if b.value is None and b.error and "non évaluées" in b.error:
                                changed = False
                                compared = True
                                verdicts.append((False, f"« {alt.text} » : lecture mal formée ({b.error})"))
                                break
                            continue
                        compared = True
                        if a.value != b.value:
                            changed = True
                            break
            if compared and not changed:
                if not any(alt.text in r for _, r in verdicts):
                    verdicts.append((False, f"« {alt.text} » : même valeur en tout point testé (sens inchangé)"))
            elif changed:
                blocking = alt.score >= LOW_PROBABILITY or u.context_dependent
                verdicts.append((blocking, f"« {alt.text} » : changerait la valeur de l'étape"
                                 + ("" if blocking else f" mais lecture peu probable ({alt.score:.2f})")))
            else:
                verdicts.append((bool(formal_affected),
                                 f"« {alt.text} » : effet sur la formalisation non déterminable automatiquement"))
        blocking = any(b for b, _ in verdicts)
        u.blocking = blocking
        u.resolution = " ; ".join(r for _, r in verdicts)
        out.append(FidelityCheck(step_id=",".join(s.id for s, _ in formal_affected) or u.line_id,
                                 kind="sensibilite_lecture", ok=not blocking,
                                 detail=f"{u.id} ({u.line_id}, « {u.chosen} ») — {u.resolution}"))
    return out


def run_fidelity(tr: Transcription, st: ProofStructure, fm: Formalization, lean: LeanReport,
                 lean_evals: dict) -> list[FidelityCheck]:
    checks: list[FidelityCheck] = []
    checks += check_anchoring(tr, st)
    checks += check_structure(st, fm)
    checks += check_coverage(tr, st)
    checks += check_fingerprints(fm, st, tr, lean_evals)
    checks += check_predicates(fm, st)
    checks += analyse_uncertainties(tr, st, fm, lean_evals)
    return checks
