"""Tests de bout en bout (exécutent réellement Lean 4 + Mathlib)."""

import json
from pathlib import Path

import pytest

from conftest import EX, needs_lean
from mathocr.pipeline import PipelineConfig, run
from mathocr.schemas import Verdict

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
    from mathocr.schemas import Reading, Uncertainty
    for alt, expected in (("0 = 0", Verdict.verified), ("0^2 = 1", Verdict.review)):
        tr = json.loads((EX / "fixtures" / "transcription.json").read_text())
        tr["uncertainties"].append(Uncertainty(
            id="U99", line_id="p1.L04", span="0^2 = 0", chosen="0^2 = 0",
            readings=[Reading(text="0^2 = 0", score=0.7), Reading(text=alt, score=0.3)],
            reason="test").model_dump())
        p = tmp_path / f"tr_{alt}.json"
        p.write_text(json.dumps(tr, ensure_ascii=False))
        r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / f"out_{alt}",
                PipelineConfig(workspace=ROOT / "lean_workspace", memory_dir=None),
                transcription=p, structure=EX / "fixtures" / "structure.json")
        assert r.verdict.verdict == expected, (alt, r.verdict.blocking_issues)
