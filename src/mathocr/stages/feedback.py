"""Retour proposé à l'élève, en français.

Le retour de base est produit par un gabarit déterministe à partir des seuls
faits établis (statuts Lean, contrôles de fidélité, observations). Un agent
peut ensuite le reformuler (voir `polish_feedback`), mais il ne reçoit que
ces faits et il lui est interdit d'en ajouter ; sa sortie est rejetée si
elle mentionne une étape inconnue ou change l'issue.
"""

from __future__ import annotations

import re

from ..schemas import Feedback, LeanReport, ProofStructure, Transcription, Verdict, VerdictReport

_STRIP_TEXT = re.compile(r"\\text\{([^}]*)\}")


def _say(latex: str) -> str:
    s = _STRIP_TEXT.sub(r"\1", latex)
    return f"${s}$" if "\\" in s or "^" in s or "_" in s else s


def build_feedback(tr: Transcription, st: ProofStructure, lean: LeanReport, vr: VerdictReport) -> Feedback:
    status = {c.step_id: c for c in lean.steps}
    strong, fix, style = [], [], []

    pattern = st.pattern
    if pattern.kind == "recurrence_simple":
        base_ok = pattern.base_step and status.get(pattern.base_step) and status[pattern.base_step].status == "verifie_elementaire"
        her_ok = pattern.heredity_step and status.get(pattern.heredity_step) and \
            status[pattern.heredity_step].status == "verifie_elementaire"
        if base_ok and her_ok:
            strong.append("La structure de la récurrence est complète : propriété définie, initialisation, "
                          "hérédité avec hypothèse de récurrence clairement utilisée, conclusion.")
        elif base_ok:
            strong.append("L'initialisation est correcte.")
    calc_ok = [s for s in st.steps if s.kind == "calcul" and status.get(s.id) and status[s.id].status == "verifie_elementaire"]
    if calc_ok:
        strong.append("Chaque égalité de la chaîne de calcul est correcte"
                      + (" et l'hypothèse de récurrence est citée au bon endroit." if any(s.justification for s in calc_ok) else "."))

    for c in lean.steps:
        s = next((x for x in st.steps if x.id == c.step_id), None)
        if s is None:
            continue
        if c.status == "refute":
            fix.append(f"L'affirmation {_say(s.statement)} ({_lines(s)}) est fausse : "
                       f"elle échoue pour {c.counterexample}. Reprends ce calcul.")
        elif c.status == "non_verifie":
            fix.append(f"L'étape {_say(s.statement)} ({_lines(s)}) n'est pas justifiée par ce qui précède : "
                       f"détaille le passage ou cite le résultat utilisé.")
        elif c.status == "verifie_agent":
            fix.append(f"L'étape {_say(s.statement)} ({_lines(s)}) est vraie mais demande un argument que la copie ne donne pas.")

    for r in vr.remarks:
        if r.startswith("Rédaction"):
            style.append(r.split(":", 1)[1].strip())
        elif "connecteur" in r:
            sid = r.split()[1]
            s = next((x for x in st.steps if x.id == sid), None)
            if s is not None and not any("découle" in x for x in style):
                style.append(f"« {s.connective} {_say(s.statement)} » : ce résultat ne découle pas de ce qui précède ; "
                             f"utilise « et » plutôt que « {s.connective} ».")

    if vr.verdict == Verdict.verified:
        summary = "Démonstration correcte et complète : chaque étape a été vérifiée."
        if style:
            summary += " Quelques points de rédaction à soigner."
    elif vr.verdict == Verdict.error:
        summary = "La démonstration contient une erreur mathématique (voir ci-dessous)."
    else:
        summary = ("La correction automatique n'a pas pu conclure ; la copie sera relue par un enseignant.")

    note = ""
    if vr.verdict != Verdict.verified:
        note = "Points à examiner :\n- " + "\n- ".join(vr.blocking_issues) if vr.blocking_issues else ""
    unc = [u for u in tr.uncertainties if u.blocking]
    if unc:
        note += ("\n" if note else "") + "Lectures incertaines à confirmer sur la copie : " + \
            ", ".join(f"{u.id} ({u.line_id} « {u.chosen} »)" for u in unc)
    # Conseil d'écriture si des lectures ont été difficiles (sans pénaliser).
    hard = [u for u in tr.uncertainties if not u.blocking and any(r.score >= 0.15 for r in u.readings[1:])]
    if len(hard) >= 3:
        examples = ", ".join(f"« {u.chosen} » pouvait se lire « {u.readings[1].text} »" for u in hard[:3])
        style.append(f"Plusieurs symboles sont ambigus à la lecture ({examples}) : soigne leur tracé.")
    return Feedback(summary=summary, points_forts=strong, points_a_corriger=fix,
                    conseils_redaction=style, note_pour_correcteur=note)


def _lines(step) -> str:
    ids = sorted({r.line_id.split(".")[-1] for r in step.source})
    return "ligne " + ", ".join(ids) if ids else "étape implicite"
