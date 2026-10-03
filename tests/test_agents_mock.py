"""Plomberie des étapes LLM avec des moteurs simulés (aucun appel réseau).

Vérifie : lecture multi-moteurs aveugle → consensus → arbitrage → structure
(avec boucle de réparation d'ancrage) → formalisation → Lean → verdict.
"""

import json

import pytest

from conftest import EX, ROOT, needs_lean
from mathocr.llm.base import Engine
from mathocr.pipeline import PipelineConfig, run
from mathocr.schemas import ProofStructure, Verdict
from mathocr.stages import agents, transcribe as tmod
from mathocr.stages.agents import WireFormalization
from mathocr.stages.transcribe import WireDecisions, WirePage

FIX = EX / "fixtures"
TR = json.loads((FIX / "transcription.json").read_text())
# Le consensus renumérote les lignes dans l'ordre de lecture : p1.L01, p1.L02…
_ORDER = sorted(TR["lines"], key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
RENUM = {ln["id"]: f"p1.L{i + 1:02d}" for i, ln in enumerate(_ORDER)}


def _renum(obj):
    txt = json.dumps(obj, ensure_ascii=False)
    for old in sorted(RENUM, key=len, reverse=True):
        txt = txt.replace(f'"{old}"', f'"@@{RENUM[old]}"')
    return json.loads(txt.replace('"@@', '"'))


def _page(variant: dict[str, str]) -> WirePage:
    lines = []
    for ln in TR["lines"]:
        x0, y0, x1, y1 = ln["bbox"]
        lines.append({"bbox": [x0 * 1000 // 1500, y0 * 1000 // 2000, x1 * 1000 // 1500, y1 * 1000 // 2000],
                      "text": variant.get(ln["id"], ln["text"]), "status": ln["status"], "confidence": 0.9})
    return WirePage(lines=lines)


class FakeEngine(Engine):
    def __init__(self, name, calls):
        self.name, self.model, self.calls = name, "simulé", calls

    def structured(self, system, text, images, schema, *, effort="high"):
        self.calls.append((self.name, schema.__name__))
        if schema is WirePage:
            # Le moteur B lit « (2n-1) » là où A lit « (2n+1) » : désaccord à arbitrer.
            return _page({"p1.L11": r"$= n^2 + (2n-1)$ (d'après H.R.)"} if self.name == "fake:B" else {})
        if schema is WireDecisions:
            return WireDecisions(decisions=[{"id": "U01", "readings": [{"text": "+", "probability": 0.97},
                                                                       {"text": "-", "probability": 0.03}],
                                             "reason": "barre verticale nette", "context_based": False}])
        if schema is ProofStructure:
            st = _renum(json.loads((FIX / "structure.json").read_text()))
            if sum(1 for c in self.calls if c[1] == "ProofStructure") == 1:
                st["steps"][2]["source"][0]["excerpt"] = "extrait inventé"  # déclenche la réparation d'ancrage
            return ProofStructure(**st)
        if schema is WireFormalization:
            fm = json.loads((FIX / "formalization.json").read_text())
            for s in fm["steps"]:
                if s.get("predicate_app"):
                    s["predicate_app"] = list(s["predicate_app"])
            return WireFormalization(**{"scopes": fm["scopes"], "steps": fm["steps"]})
        raise AssertionError(schema)


@pytest.fixture
def fake(monkeypatch):
    calls = []
    mk = lambda spec, cache=None, **kw: FakeEngine(spec, calls)  # noqa: E731
    monkeypatch.setattr(tmod, "make_engine", mk)
    monkeypatch.setattr(agents, "make_engine", mk)
    return calls


@needs_lean
@pytest.mark.lean
def test_full_llm_path_with_fake_engines(tmp_path, fake):
    cfg = PipelineConfig(workspace=ROOT / "lean_workspace", ocr_engines=["fake:A", "fake:B"],
                         reasoning_engine="fake:R", cache_dir=tmp_path / "cache")
    r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / "out", cfg)
    # Deux lectures aveugles, un arbitrage, structure réparée une fois ; aucune formalisation
    # par l'agent : toutes les étapes de cette copie se traduisent sans LLM.
    kinds = [k for _, k in fake]
    assert kinds.count("WirePage") == 2 and kinds.count("WireDecisions") == 1
    assert kinds.count("ProofStructure") == 2 and kinds.count("WireFormalization") == 0
    u = next(u for u in r.transcription.uncertainties if u.line_id.endswith("L12") or "L1" in u.line_id)
    assert u.readings[0].text == "+" and "arbitre:fake:R" in u.readings[0].support
    assert r.verdict.verdict == Verdict.verified, r.verdict.blocking_issues


def test_tier2_uses_given_engine_and_only_selected_steps(monkeypatch):
    from mathocr.schemas import (Formalization, LeanReport, ProofStep, ProofStructure as PS, ReferenceStatement,
                                 SourceRef, StepCheck, StepFormal)
    ref = ReferenceStatement(exercise_id="t", statement_latex="", lean_statement="True")
    st = PS(steps=[ProofStep(id=i, kind="affirmation", statement="x", source=[SourceRef(line_id="p1.L01", excerpt="x")])
                   for i in ("s1", "s2")], scopes=[], pattern={"kind": "aucun"})
    fm = Formalization(scopes=[], steps=[StepFormal(step_id=i, role="prop", claim="1 = 1") for i in ("s1", "s2")])
    lean = LeanReport(file="", steps=[StepCheck(step_id=i, decl=i, status="non_verifie") for i in ("s1", "s2")])
    used = []

    class E:
        def structured(self, system, text, images, schema, **kw):
            return agents.WireTier2(proof="rfl")

    monkeypatch.setattr(agents, "_engine", lambda cfg, spec=None: used.append(spec) or E())
    out = agents.tier2_attempts(ref, st, fm, lean, PipelineConfig(reasoning_engine="fort"), engine="bon_marche",
                                only={"s2"})
    assert used == ["bon_marche"]
    assert out.of("s1").agent_proof is None and out.of("s2").agent_proof == "rfl"
