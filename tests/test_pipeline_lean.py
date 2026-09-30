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
