"""Tests de bout en bout (exécutent réellement Lean 4 + Mathlib)."""

import json
from pathlib import Path

import pytest

from conftest import EX, needs_lean
from leanonsteroids.pipeline import PipelineConfig, run
from leanonsteroids.schemas import Verdict

ROOT = Path(__file__).resolve().parents[1]
CASES = ["fixtures"] + sorted(p.name for p in (EX / "variantes").iterdir() if p.is_dir())


def _run(tmp_path, d: Path):
    return run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path, PipelineConfig(workspace=ROOT / "lean_workspace"),
               transcription=d / "transcription.json", structure=d / "structure.json",
               formalization=d / "formalization.json")


@needs_lean
@pytest.mark.lean
@pytest.mark.parametrize("case", CASES)
def test_expected_verdicts(tmp_path, case):
    d = EX / case if case == "fixtures" else EX / "variantes" / case
    expected = json.loads((d / "attendu.json").read_text())["issue"]
    r = _run(tmp_path, d)
    assert r.verdict.verdict.value == expected, r.verdict.blocking_issues
    assert (tmp_path / "rapport.html").exists()


@needs_lean
@pytest.mark.lean
def test_sorry_in_formalization_is_refused(tmp_path):
    fm = json.loads((EX / "fixtures" / "formalization.json").read_text())
    next(s for s in fm["steps"] if s["step_id"] == "S12")["claim"] = "n ^ 2 + (2 * n + 1) = (n + 1) ^ 2 := by sorry"
    p = tmp_path / "fm.json"
    p.write_text(json.dumps(fm, ensure_ascii=False))
    r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / "out", PipelineConfig(workspace=ROOT / "lean_workspace"),
            transcription=EX / "fixtures" / "transcription.json", structure=EX / "fixtures" / "structure.json",
            formalization=p)
    assert r.verdict.verdict == Verdict.review
    assert r.lean.run is None  # rien n'est compilé quand un fragment est refusé
    assert any("sorry" in v.token for v in r.lean.policy_violations)


@needs_lean
@pytest.mark.lean
def test_trivialized_step_is_caught(tmp_path):
    """Un formaliseur qui remplace une étape fausse par `True` ne doit pas obtenir « vérifié »."""
    d = EX / "variantes" / "erreur_calcul"
    fm = json.loads((d / "formalization.json").read_text())
    s11 = next(s for s in fm["steps"] if s["step_id"] == "S11")
    s11["claim"], s11["sides"] = "True", None
    p = tmp_path / "fm.json"
    p.write_text(json.dumps(fm, ensure_ascii=False))
    r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / "out", PipelineConfig(workspace=ROOT / "lean_workspace"),
            transcription=d / "transcription.json", structure=d / "structure.json", formalization=p)
    assert r.verdict.verdict != Verdict.verified
    assert any("S11" in b for b in r.verdict.blocking_issues + r.verdict.reasons)


@needs_lean
@pytest.mark.lean
def test_unvalidated_statement_never_verified(tmp_path):
    ex = json.loads((EX / "exercice.json").read_text())
    ex["validated_by"] = None
    p = tmp_path / "ex.json"
    p.write_text(json.dumps(ex, ensure_ascii=False))
    r = run(p, [EX / "copie_p1.webp"], tmp_path / "out", PipelineConfig(workspace=ROOT / "lean_workspace"),
            transcription=EX / "fixtures" / "transcription.json", structure=EX / "fixtures" / "structure.json",
            formalization=EX / "fixtures" / "formalization.json")
    assert r.verdict.verdict == Verdict.review


# Avec la traduction déterministe, la variante « formalisation infidèle » disparaît :
# le code traduit ce que l'élève a écrit, donc l'erreur est établie.
DETERMINISTIC_EXPECTED = {
    "fixtures": "raisonnement vérifié",
    "erreur_calcul": "erreur mathématique établie",
    "formalisation_infidele": "erreur mathématique établie",
    "lecture_ambigue": "examen nécessaire",
    "saut_logique": "examen nécessaire",
}


@needs_lean
@pytest.mark.lean
@pytest.mark.parametrize("case", sorted(DETERMINISTIC_EXPECTED))
def test_deterministic_formalization_verdicts(tmp_path, case):
    d = EX / case if case == "fixtures" else EX / "variantes" / case
    r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path, PipelineConfig(workspace=ROOT / "lean_workspace"),
            transcription=d / "transcription.json", structure=d / "structure.json")
    assert r.formalization.provenance.startswith("traduction déterministe")
    assert r.verdict.verdict.value == DETERMINISTIC_EXPECTED[case], r.verdict.blocking_issues


@needs_lean
@pytest.mark.lean
def test_broken_definition_never_yields_a_conclusion(tmp_path):
    """Cas réel (essai OpenRouter) : une définition qui ne compile pas avait permis une fausse
    « erreur établie ». Une formalisation cassée doit toujours mener à « examen nécessaire »."""
    fm = json.loads((EX / "fixtures" / "formalization.json").read_text())
    s01 = next(s for s in fm["steps"] if s["step_id"] == "S01")
    s01["def_body"] = "∑ h ∈ Finset.range n, (2 * k + 1) = n ^ 2"  # k inconnu
    # Une « réfutation » d'agent qui ne tient que grâce à la définition cassée.
    next(s for s in fm["steps"] if s["step_id"] == "S05")["agent_refutation"] = "intro h\nsimp_all [P]"
    p = tmp_path / "fm.json"
    p.write_text(json.dumps(fm, ensure_ascii=False))
    r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / "out",
            PipelineConfig(workspace=ROOT / "lean_workspace", memory_dir=None),
            transcription=EX / "fixtures" / "transcription.json", structure=EX / "fixtures" / "structure.json",
            formalization=p)
    assert r.verdict.verdict == Verdict.review
    assert not any(c.status in ("refute", "verifie_agent") for c in r.lean.steps)
    assert not any(c.status == "verifie_elementaire" and "P" in (c.lean_snippet or "").split(":")[-1]
                   for c in r.lean.steps if c.step_id in ("S05", "S14"))


@needs_lean
@pytest.mark.lean
def test_doubt_that_cannot_change_verdict_is_cleared_by_lean(tmp_path):
    """Cas réel : « 0^2 = 0 » pouvait se lire « 0 = 0 » (15 %). Les deux lectures sont vraies et la copie
    reste vérifiée : Lean le confirme, le doute ne bloque plus. « 0^2 = 1 » changerait le verdict."""
    from leanonsteroids.schemas import Reading, Uncertainty
    # Une « rature » déclarée par l'arbitre n'efface jamais un symbole remplacé (0 → 1).
    for alt, rature, expected in (("0 = 0", False, Verdict.verified), ("0^2 = 1", False, Verdict.review),
                                  ("0^2 = 1", True, Verdict.review)):
        tr = json.loads((EX / "fixtures" / "transcription.json").read_text())
        tr["uncertainties"].append(Uncertainty(
            id="U99", line_id="p1.L04", span="0^2 = 0", chosen="0^2 = 0", rature=rature,
            readings=[Reading(text="0^2 = 0", score=0.7), Reading(text=alt, score=0.3)],
            reason="test").model_dump())
        p = tmp_path / f"tr_{alt}_{rature}.json"
        p.write_text(json.dumps(tr, ensure_ascii=False))
        r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / f"out_{alt}_{rature}",
                PipelineConfig(workspace=ROOT / "lean_workspace", memory_dir=None),
                transcription=p, structure=EX / "fixtures" / "structure.json")
        assert r.verdict.verdict == expected, (alt, r.verdict.blocking_issues)


def _tvi_case(proof: str):
    from leanonsteroids.schemas import (Formalization, ProofStep, ProofStructure, ReferenceStatement, SourceRef,
                                 StepFormal)
    ref = ReferenceStatement(exercise_id="tvi", statement_latex="", lean_statement="True",
                             lean_imports=["MathOCRCheck.Prelude", "Mathlib.Topology.Order.IntermediateValue",
                                           "Mathlib.Topology.Instances.Real.Lemmas"])
    st = ProofStructure(scopes=[], pattern={"kind": "aucun"}, steps=[
        ProofStep(id="s1", kind="affirmation", statement="d'après le TVI, f s'annule",
                  source=[SourceRef(line_id="p1.L01", excerpt="TVI")])])
    claim = "∀ (f : ℝ → ℝ), Continuous f → f 0 < 0 → 0 < f 1 → ∃ c, f c = 0"
    fm = Formalization(scopes=[], steps=[StepFormal(step_id="s1", role="prop", claim=claim, agent_proof=proof,
                                                    cites=["tvi"])])
    return ref, st, fm


@needs_lean
@pytest.mark.lean
@pytest.mark.parametrize("proof,expected", [
    # s'appuie sur le théorème cité : étape justifiée
    ("intro f hf h0 h1\nobtain ⟨c, hc⟩ := intermediate_value_univ 0 1 hf "
     "(show (0:ℝ) ∈ Set.Icc (f 0) (f 1) from ⟨h0.le, h1.le⟩)\nexact ⟨c, hc⟩", "verifie_theoreme"),
    # passe par un autre grand théorème (connexité), non cité : saut logique
    ("intro f hf h0 h1\nobtain ⟨c, -, hc⟩ := isPreconnected_univ.intermediate_value (Set.mem_univ 0) "
     "(Set.mem_univ 1) hf.continuousOn (show (0:ℝ) ∈ Set.Icc (f 0) (f 1) from ⟨h0.le, h1.le⟩)\nexact ⟨c, hc⟩",
     "verifie_agent"),
])
def test_cited_theorem_is_accepted_only_if_used_alone(tmp_path, monkeypatch, proof, expected):
    from leanonsteroids import theoremes
    from leanonsteroids.lean.sandbox import SandboxConfig
    from leanonsteroids.lean.verify import verify

    cat = theoremes.Catalogue({"tvi": theoremes.Theoreme(
        cle="tvi", nom="théorème des valeurs intermédiaires", alias=["TVI"],
        lemmes=["intermediate_value_univ", "intermediate_value_Icc"], compagnons=["ordered_connected_space"],
        imports=["Mathlib.Topology.Order.IntermediateValue"], verifie=True)},
        ["Mathlib.Topology", "Mathlib.Analysis"], {"Continuous.continuousOn"})
    monkeypatch.setattr(theoremes, "charger", lambda path=None: cat)
    ref, st, fm = _tvi_case(proof)
    lean, _ = verify(ref, st, fm, SandboxConfig(workspace=ROOT / "lean_workspace"), tmp_path)
    assert lean.steps[0].status == expected, lean.steps[0]


@needs_lean
@pytest.mark.lean
def test_exercise_context_is_available_to_steps_and_assembly(tmp_path):
    from leanonsteroids.lean.sandbox import SandboxConfig
    from leanonsteroids.lean.verify import verify
    from leanonsteroids.schemas import (Formalization, ProofStep, ProofStructure, ReferenceStatement, SourceRef,
                                 StepFormal)
    ref = ReferenceStatement(
        exercise_id="ctx", statement_latex="Soit f telle que f(x)² = a pour tout x. Montrer que f(0)² = a.",
        lean_statement="∀ (f : ℝ → ℝ) (a : ℝ), 0 ≤ a → (∀ x, f x ^ 2 = a) → f 0 ^ 2 = a",
        contexte={"objets": [{"nom": "f", "type": "ℝ → ℝ"}, {"nom": "a", "type": "ℝ"}],
                  "hypotheses": [{"nom": "h_a", "lean": "0 ≤ a"}, {"nom": "h_eq", "lean": "∀ x, f x ^ 2 = a"}]})
    st = ProofStructure(scopes=[], pattern={"kind": "direct", "conclusion_step": "s1"}, steps=[
        ProofStep(id="s1", kind="conclusion", statement="f(0)^2 = a", source=[SourceRef(line_id="p1.L01", excerpt="x")])])
    fm = Formalization(scopes=[], steps=[StepFormal(step_id="s1", role="prop", claim="f 0 ^ 2 = a")])
    lean, _ = verify(ref, st, fm, SandboxConfig(workspace=ROOT / "lean_workspace"), tmp_path)
    assert lean.steps[0].status == "verifie_elementaire", lean.steps[0]
    assert lean.assembly_ok and lean.statement_match


@needs_lean
@pytest.mark.lean
@pytest.mark.parametrize("claim,cites_copie,closed", [
    # résultat élémentaire du cours : admis sans citation
    ("∀ x : ℝ, f x = Real.sqrt a ∨ f x = -Real.sqrt a", [], True),
    # le TVI du kit : seulement si la copie cite le TVI quelque part
    ("∀ x y : ℝ, f x ≤ 0 → 0 ≤ f y → ∃ c, f c = 0", [], False),
    ("∀ x y : ℝ, f x ≤ 0 → 0 ≤ f y → ∃ c, f c = 0", ["tvi"], True),
])
def test_exercise_kit_result_closes_a_step(tmp_path, claim, cites_copie, closed):
    from leanonsteroids.lean.sandbox import SandboxConfig
    from leanonsteroids.lean.verify import verify
    from leanonsteroids.schemas import Formalization, ProofStep, ProofStructure, ReferenceStatement, SourceRef, StepFormal
    ref = ReferenceStatement(**json.loads((ROOT / "examples/collecte/exercices/mw06.json").read_text()))
    st = ProofStructure(scopes=[], pattern={"kind": "aucun"}, steps=[
        ProofStep(id="s1", kind="affirmation", statement="f s'annule", source=[SourceRef(line_id="p1.L01", excerpt="x")])])
    fm = Formalization(scopes=[], cites_copie=cites_copie,
                       steps=[StepFormal(step_id="s1", role="prop", claim=claim)])
    lean, _ = verify(ref, st, fm, SandboxConfig(workspace=ROOT / "lean_workspace"), tmp_path)
    c = lean.steps[0]
    if closed:
        assert c.status == "verifie_elementaire" and "kit" in (c.closed_by or ""), c
    else:
        assert c.status == "non_verifie", c


def _glue_case(glue: str):
    from leanonsteroids.schemas import Formalization, ProofStep, ProofStructure, ReferenceStatement, SourceRef, StepFormal
    ref = ReferenceStatement(exercise_id="abs", statement_latex="Si x² = 0 alors x = 0.",
                             lean_statement="∀ (x : ℝ), x ^ 2 = 0 → x = 0",
                             contexte={"objets": [{"nom": "x", "type": "ℝ"}],
                                       "hypotheses": [{"nom": "h_eq", "lean": "x ^ 2 = 0"}]})
    src = [SourceRef(line_id="p1.L01", excerpt="x")]
    st = ProofStructure(scopes=[{"id": "abs", "assumptions": ["s1"]}], pattern={"kind": "libre", "conclusion_step": "s3"},
                        steps=[ProofStep(id="s1", kind="hypothese", scope="abs", statement="x ≠ 0", source=src),
                               ProofStep(id="s2", kind="affirmation", scope="abs", statement="x^2 > 0", depends_on=["s1"],
                                         source=src),
                               ProofStep(id="s3", kind="conclusion", statement="x = 0", source=src)])
    fm = Formalization(scopes=[], assemblage_agent=glue, steps=[
        StepFormal(step_id="s1", role="hyp", claim="x ≠ 0"),
        StepFormal(step_id="s2", role="prop", claim="0 < x ^ 2", uses=["s1"]),
        StepFormal(step_id="s3", role="none", not_formalized_reason="conclusion par l'absurde")])
    return ref, st, fm


@needs_lean
@pytest.mark.lean
@pytest.mark.parametrize("glue,accepted", [
    ("intro x hx\nby_contra h\nhave h2 := Copie.s2 x hx h\nlinarith", True),          # absurde, logique seule
    ("intro x hx\nexact pow_eq_zero_iff (two_ne_zero) |>.mp hx", False),                # ignore la copie
    ("intro x hx\nby_contra h\nhave h2 := Copie.s2 x hx h\nnlinarith", False),          # tactique interdite
])
def test_agent_assembly_is_checked(tmp_path, glue, accepted):
    from leanonsteroids.lean.sandbox import SandboxConfig
    from leanonsteroids.lean.verify import verify
    ref, st, fm = _glue_case(glue)
    lean, _ = verify(ref, st, fm, SandboxConfig(workspace=ROOT / "lean_workspace"), tmp_path)
    assert lean.steps[1].status == "verifie_elementaire", lean.steps[1]
    assert (lean.assemblage_agent or "").startswith("accepté") is accepted, lean.assemblage_agent
    assert lean.statement_match is accepted


@needs_lean
@pytest.mark.lean
def test_agent_assembly_axioms_are_the_ones_checked(tmp_path):
    from leanonsteroids.lean.sandbox import SandboxConfig
    from leanonsteroids.lean.verify import verify
    ref, st, fm = _glue_case("intro x hx\nby_contra h\nhave h2 := Copie.s2 x hx h\nlinarith")
    lean, _ = verify(ref, st, fm, SandboxConfig(workspace=ROOT / "lean_workspace"), tmp_path)
    assert lean.assembly_ok and lean.axioms_ok  # l'assemblage automatique (échoué) ne compte plus


@needs_lean
@pytest.mark.lean
def test_refutation_respects_the_case_hypothesis(tmp_path):
    """Cas réel (B07) : « pour x ∈ [0,1[ … |xⁿ − f(x)| = xⁿ » était « réfutée » en x = −1, car l'hypothèse
    du cas n'était pas passée à la réfutation. Une étape vraie dans son cas n'est jamais déclarée fausse."""
    from leanonsteroids.lean.sandbox import SandboxConfig
    from leanonsteroids.lean.verify import verify
    from leanonsteroids.schemas import (Formalization, ProofStep, ProofStructure, ReferenceStatement, ScopeFormal,
                                 SourceRef, StepFormal)
    ref = ReferenceStatement(exercise_id="cas", statement_latex="", lean_statement="True")
    src = [SourceRef(line_id="p1.L01", excerpt="x")]
    st = ProofStructure(
        scopes=[{"id": "cas", "parent": "global", "variables": ["x"], "assumptions": ["s1"]}],
        pattern={"kind": "aucun"},
        steps=[ProofStep(id="s1", kind="hypothese", scope="cas", statement="x ∈ [0,1[", source=src),
               ProofStep(id="s2", kind="calcul", scope="cas", statement="|x| = x", source=src)])
    refutation = "intro h\nhave := h (-1)\nnorm_num at this"  # valable seulement sans l hypothèse du cas
    fm = Formalization(scopes=[ScopeFormal(scope_id="cas", binders=[{"name": "x", "type": "ℝ"}])], steps=[
        StepFormal(step_id="s1", role="hyp", claim="x ∈ Set.Ico (0:ℝ) 1"),
        StepFormal(step_id="s2", role="prop", claim="|x| = x", agent_refutation=refutation)])
    lean, _ = verify(ref, st, fm, SandboxConfig(workspace=ROOT / "lean_workspace"), tmp_path)
    assert next(c for c in lean.steps if c.step_id == "s2").status != "refute"
