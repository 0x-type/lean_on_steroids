"""Mémoire partagée : une étape traduite par l'agent et validée n'est payée qu'une fois."""

import json

import pytest

from conftest import EX, ROOT, needs_lean
from mathocr.llm.base import Engine
from mathocr.pipeline import PipelineConfig, run
from mathocr.schemas import Verdict
from mathocr.stages import agents
from mathocr.stages.agents import WireFormalization, WireTier2

FIX = EX / "fixtures"


class FakeFormalizer(Engine):
    name, model = "fake:F", "simulé"

    def __init__(self, calls, claim="P (n + 1)"):
        self.calls, self.claim = calls, claim

    def structured(self, system, text, images, schema, *, effort="high"):
        if schema is WireTier2:  # niveau 2 : l'agent simulé n'a pas de preuve à proposer
            return WireTier2()
        assert schema is WireFormalization
        self.calls.append(text)
        return WireFormalization(scopes=[{"scope_id": "H", "binders": [{"name": "n", "type": "ℕ"}]}],
                                 steps=[{"step_id": "S14", "role": "prop", "claim": self.claim,
                                         "uses": ["S13"], "predicate_app": ["P", "n+1"]}])


def _structure(tmp_path):
    st = json.loads((FIX / "structure.json").read_text())
    # Formulation que le traducteur déterministe refuse (« bien ») : il faut l'agent.
    next(s for s in st["steps"] if s["id"] == "S14")["statement"] = r"P(n+1) \text{ est bien vraie}"
    p = tmp_path / "structure.json"
    p.write_text(json.dumps(st, ensure_ascii=False))
    return p


def _run(tmp_path, name, st_path, memory_dir):
    cfg = PipelineConfig(workspace=ROOT / "lean_workspace", reasoning_engine="fake:F",
                         cache_dir=tmp_path / f"cache_{name}", memory_dir=memory_dir)
    return run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / name, cfg,
               transcription=FIX / "transcription.json", structure=st_path)


@needs_lean
@pytest.mark.lean
def test_validated_agent_step_is_reused(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(agents, "make_engine", lambda spec, cache=None, **kw: FakeFormalizer(calls))
    st = _structure(tmp_path)
    mem = tmp_path / "memoire"

    r1 = _run(tmp_path, "eleve1", st, mem)
    assert len(calls) == 1 and "S14" in calls[0]
    assert r1.formalization.of("S14").origin == "agent"
    assert r1.verdict.verdict == Verdict.verified, r1.verdict.blocking_issues

    # Autre élève, même étape : aucun appel (le cache d'appels est vide, c'est la mémoire qui sert).
    r2 = _run(tmp_path, "eleve2", st, mem)
    assert len(calls) == 1
    assert r2.formalization.of("S14").origin == "mémoire"
    assert r2.verdict.verdict == Verdict.verified


@needs_lean
@pytest.mark.lean
def test_unfaithful_agent_step_is_not_remembered(tmp_path, monkeypatch):
    calls = []
    # L'agent traduit « P(n+1) » par « P n » : le contrôle des prédicats le rejette.
    monkeypatch.setattr(agents, "make_engine", lambda spec, cache=None, **kw: FakeFormalizer(calls, "P n"))
    st = _structure(tmp_path)
    mem = tmp_path / "memoire"
    r1 = _run(tmp_path, "eleve1", st, mem)
    assert r1.verdict.verdict != Verdict.verified
    assert not list((mem / "somme_impairs" / "etapes").glob("*.json"))
    _run(tmp_path, "eleve2", st, mem)
    assert len(calls) == 2  # rien de mémorisé : l'agent est rappelé
