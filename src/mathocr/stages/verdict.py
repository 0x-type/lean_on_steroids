"""Décision finale : trois issues, jamais une de plus, jamais par défaut optimiste.

* **raisonnement vérifié** — toutes les conditions suivantes :
  énoncé de référence validé ; aucun fragment interdit ; chaque affirmation de
  la copie est vérifiée par l'automatisation *élémentaire* à partir de ses
  seules dépendances ; l'assemblage des étapes et l'accord avec l'énoncé sont
  vérifiés ; aucun axiome hors liste ; tous les contrôles de fidélité passent ;
  aucune incertitude de lecture n'est bloquante.

* **erreur mathématique établie** — au moins une étape est *réfutée* dans Lean
  (sa négation est démontrée, contre-exemple à l'appui) ET cette étape est
  fidèle à la copie (ancrage, empreinte numérique ou prédicat) ET aucune
  lecture ambiguë ne touche cette étape ET l'énoncé de référence est validé.

* **examen nécessaire** — dans tous les autres cas. En particulier, un échec
  de Lean (étape non vérifiée) ne suffit jamais à conclure que l'élève a tort.
"""

from __future__ import annotations

from ..schemas import (
    FidelityCheck,
    Formalization,
    LeanReport,
    ProofStructure,
    ReferenceStatement,
    Transcription,
    Verdict,
    VerdictReport,
)

MIN_LINE_CONFIDENCE = 0.5


def decide(
    ref: ReferenceStatement,
    tr: Transcription,
    st: ProofStructure,
    lean: LeanReport,
    fidelity: list[FidelityCheck],
    formal: Formalization,
) -> VerdictReport:
    blocking: list[str] = []
    remarks: list[str] = []
    reasons: list[str] = []

    if not ref.validated_by:
        blocking.append("La formalisation de l'énoncé n'a pas été validée par un enseignant.")
    for v in lean.policy_violations:
        blocking.append(f"Fragment Lean refusé ({v.where}) : « {v.token} » — {v.detail}.")
    if lean.run is None and not lean.policy_violations:
        blocking.append("Lean n'a pas été exécuté.")
    if lean.run is not None and lean.run.timed_out:
        blocking.append(f"Lean a dépassé le délai ({lean.run.seconds:.0f} s).")

    # Fidélité, étape par étape
    fid_bad: dict[str, list[str]] = {}
    fid_ok: dict[str, set[str]] = {}
    for c in fidelity:
        if c.ok is False:
            for sid in c.step_id.split(","):
                fid_bad.setdefault(sid, []).append(f"{c.kind} : {c.detail}")
        elif c.ok:
            for sid in c.step_id.split(","):
                fid_ok.setdefault(sid, set()).add(c.kind)
    for sid, probs in fid_bad.items():
        for p in probs:
            blocking.append(f"Fidélité ({sid}) — {p}")

    # Incertitudes de lecture
    uncertain_steps: set[str] = set()
    for u in tr.uncertainties:
        if u.blocking:
            for c in fidelity:
                if c.kind == "sensibilite_lecture" and c.detail.startswith(u.id + " "):
                    uncertain_steps |= set(c.step_id.split(","))
        elif u.resolution and "peu probable" in u.resolution and "changerait" in u.resolution:
            remarks.append(f"Lecture {u.id} ({u.line_id}) : {u.resolution}.")

    # Étapes
    refuted, unverified, agent_only, formal_errors = [], [], [], []
    for c in lean.steps:
        if c.status == "refute":
            refuted.append(c)
        elif c.status == "non_verifie":
            unverified.append(c)
        elif c.status == "verifie_agent":
            agent_only.append(c)
        elif c.status == "erreur_formalisation":
            formal_errors.append(c)
    for c in formal_errors:
        blocking.append(f"L'énoncé Lean de l'étape {c.step_id} ne compile pas : la formalisation est à reprendre.")
    for c in unverified:
        blocking.append(f"Étape {c.step_id} ni vérifiée ni réfutée par l'automatisation élémentaire "
                        f"(saut logique, justification manquante ou limite de l'outil).")
    for c in agent_only:
        blocking.append(f"Étape {c.step_id} vraie mais seulement avec une preuve non élémentaire absente de la copie (saut logique).")
    step_failures = bool(refuted or unverified or agent_only or formal_errors)
    if lean.run is not None and not lean.assembly_ok and not step_failures:
        blocking.append("Les étapes de la copie ne s'assemblent pas en une démonstration de sa conclusion.")
    if lean.run is not None and not lean.statement_match and not step_failures:
        blocking.append("La conclusion de la copie n'entraîne pas l'énoncé à démontrer.")
    if lean.run is not None and not lean.assembly_ok and step_failures and not refuted:
        blocking.append("La démonstration assemblée repose sur les étapes non vérifiées ci-dessus.")

    # Toute étape formalisée non implicite doit avoir une preuve positive de fidélité :
    # sinon rien n'empêche une formalisation de remplacer l'étape par un énoncé trivial.
    evidence = {"empreinte_numerique", "application_predicat", "retrotraduction"}
    for f in formal.steps:
        step = next((s for s in st.steps if s.id == f.step_id), None)
        if step is None or step.implicit or f.role == "none":
            continue
        if f.step_id in fid_bad:
            continue
        if not (fid_ok.get(f.step_id, set()) & evidence):
            blocking.append(f"Fidélité ({f.step_id}) — aucune preuve positive que la formalisation dit ce que dit "
                            f"la copie (ni empreinte numérique, ni prédicat, ni rétro-traduction).")
    if lean.run is not None and lean.assembly_ok and not lean.axioms_ok:
        blocking.append("Axiomes non autorisés dans la démonstration assemblée.")

    lean_ok = {c.step_id for c in lean.steps if c.status in ("verifie_elementaire", "verifie_theoreme", "hypothese",
                                                           "definition", "non_formalise")}
    for c in lean.steps:
        if c.status == "verifie_theoreme":
            remarks.append(f"Étape {c.step_id} justifiée par un {c.closed_by} — vérifié dans Lean.")
        elif c.status == "verifie_elementaire" and c.closed_by and "niveau 2" in c.closed_by:
            remarks.append(f"Étape {c.step_id} admise sans justification écrite : Lean la démontre par un "
                           f"argument élémentaire.")
    for o in st.observations:
        tag = ", ".join(o.step_ids)
        if o.severity in ("lacune", "erreur") and not (o.step_ids and set(o.step_ids) <= lean_ok):
            blocking.append(f"Observation ({tag}, non certifiée par Lean) : {o.text}")
        elif o.severity in ("lacune", "erreur"):
            # Lean a vérifié ces étapes à partir de ce que l'élève a écrit : la remarque porte sur la
            # présentation, pas sur la validité.
            remarks.append(f"Rédaction ({tag}, logique vérifiée par Lean) : {o.text}")
        else:
            remarks.append(f"Rédaction ({tag}) : {o.text}")
    for c in lean.steps:
        step = next((s for s in st.steps if s.id == c.step_id), None)
        if c.independent_of_deps and step and step.connective in ("donc", "alors", "d'où", "ainsi") \
                and c.status == "verifie_elementaire":
            remarks.append(f"Étape {c.step_id} : Lean la démontre sans utiliser ce qui précède ; "
                           f"le connecteur « {step.connective} » n'est pas justifié.")
    for s in st.steps:
        if s.implicit:
            remarks.append(f"Étape {s.id} implicite dans la copie ({s.implicit_reason}) : "
                           f"ajoutée pour la vérification, sans contenu nouveau.")

    # --- Erreur établie ? ---
    # Une formalisation qui ne compile pas (définition ou énoncé) rend tout résultat Lean suspect :
    # aucune erreur ne peut alors être « établie ».
    established = []
    if formal_errors:
        for c in refuted:
            blocking.append(f"Étape {c.step_id} réfutée par Lean, mais la formalisation contient des erreurs : "
                            f"réfutation non retenue.")
        refuted = []
    for c in refuted:
        step = next((s for s in st.steps if s.id == c.step_id), None)
        faithful = c.step_id not in fid_bad and bool(fid_ok.get(c.step_id, set()) & {
            "empreinte_numerique", "application_predicat", "retrotraduction"})
        anchored = "ancrage" in fid_ok.get(c.step_id, set())
        lines_ok = step is not None and all(
            tr.line(r.line_id).confidence >= MIN_LINE_CONFIDENCE for r in step.source if r.excerpt)
        if faithful and anchored and lines_ok and c.step_id not in uncertain_steps and ref.validated_by:
            established.append(c)
        else:
            why = []
            if not faithful:
                why.append("fidélité de la formalisation non établie")
            if not anchored:
                why.append("ancrage dans la copie non établi")
            if not lines_ok:
                why.append("lecture de la ligne peu sûre")
            if c.step_id in uncertain_steps:
                why.append("lecture ambiguë bloquante")
            blocking.append(f"Étape {c.step_id} réfutée par Lean (contre-exemple {c.counterexample}), "
                            f"mais l'erreur n'est pas établie : {', '.join(why)}.")

    if established:
        for c in established:
            step = st.step(c.step_id)
            reasons.append(f"Étape {c.step_id} fausse : « {step.statement} » — Lean démontre sa négation "
                           f"(contre-exemple : {c.counterexample}). Transcription et formalisation contrôlées.")
        return VerdictReport(verdict=Verdict.error, reasons=reasons, blocking_issues=blocking, remarks=remarks)

    if not blocking and uncertain_steps:
        blocking.append("Lecture ambiguë bloquante sur les étapes " + ", ".join(sorted(uncertain_steps)) + ".")
    if not blocking:
        n_checked = sum(1 for c in lean.steps if c.status == "verifie_elementaire")
        reasons.append(f"{n_checked} affirmations vérifiées une à une dans Lean 4 à partir de leurs seules dépendances.")
        reasons.append("Assemblage des étapes et accord avec l'énoncé de référence vérifiés ; axiomes standard uniquement.")
        reasons.append("Transcription et formalisation contrôlées (ancrage, empreintes numériques, prédicats, lectures ambiguës).")
        return VerdictReport(verdict=Verdict.verified, reasons=reasons, blocking_issues=[], remarks=remarks)

    reasons.append("Au moins un point empêche de conclure automatiquement (voir la liste).")
    return VerdictReport(verdict=Verdict.review, reasons=reasons, blocking_issues=blocking, remarks=remarks)
