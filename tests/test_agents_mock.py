"""Plomberie des étapes LLM avec des moteurs simulés (aucun appel réseau).

Vérifie : lecture multi-moteurs aveugle → consensus → arbitrage → structure
(avec boucle de réparation d'ancrage) → formalisation → Lean → verdict.
"""

import json

import pytest

from conftest import EX, ROOT, needs_lean
from leanonsteroids.llm.base import Engine
from leanonsteroids.pipeline import PipelineConfig, run
from leanonsteroids.schemas import ProofStructure, Verdict
from leanonsteroids.stages import agents, transcribe as tmod
from leanonsteroids.stages.agents import WireFormalization
from leanonsteroids.stages.transcribe import WireDecisions, WirePage

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
                                             "reason": "barre verticale nette", "context_based": False,
                                             "rature": False}])
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
        if schema.__name__ in ("WireBacks", "WireCmps"):  # juge : relit et compare (ici, rien à signaler)
            return schema(items=[])
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
    from leanonsteroids.schemas import (Formalization, LeanReport, ProofStep, ProofStructure as PS, ReferenceStatement,
                                 SourceRef, StepCheck, StepFormal)
    ref = ReferenceStatement(exercise_id="t", statement_latex="", lean_statement="True")
    st = PS(steps=[ProofStep(id=i, kind="affirmation", statement="x", source=[SourceRef(line_id="p1.L01", excerpt="x")])
                   for i in ("s1", "s2")], scopes=[], pattern={"kind": "aucun"})
    fm = Formalization(scopes=[], steps=[StepFormal(step_id=i, role="prop", claim="1 = 1") for i in ("s1", "s2")])
    lean = LeanReport(file="", steps=[StepCheck(step_id=i, decl=i, status="non_verifie") for i in ("s1", "s2")])
    used = []

    class E:
        def structured(self, system, text, images, schema, **kw):
            from leanonsteroids.llm.pricing import record
            record(engine="e", model="m", input_tokens=1, output_tokens=1, cost_usd=0.5)
            return agents.WireTier2(proof="rfl")

    monkeypatch.setattr(agents, "_engine", lambda cfg, spec=None: used.append(spec) or E())
    from leanonsteroids.llm.pricing import ledger_scope
    with ledger_scope() as led:
        out = agents.tier2_attempts(ref, st, fm, lean, PipelineConfig(reasoning_engine="fort"),
                                    engine="bon_marche", only={"s2"})
    assert used == ["bon_marche"]
    assert led.report().total_usd == 0.5  # le coût d'un appel fait dans un fil parallèle est compté
    assert out.of("s1").agent_proof is None and out.of("s2").agent_proof == "rfl"


def test_inadmissible_tier2_fragment_is_dropped_not_fatal():
    assert agents._admissible("  norm_num  ", "x") == "norm_num"
    assert agents._admissible("sorry", "x") is None
    assert agents._admissible("", "x") is None


def test_tier2_engine_failure_leaves_step_unverified(monkeypatch):
    from leanonsteroids.llm.base import EngineError
    from leanonsteroids.schemas import (Formalization, LeanReport, ProofStep, ProofStructure as PS, ReferenceStatement,
                                 SourceRef, StepCheck, StepFormal)
    ref = ReferenceStatement(exercise_id="t", statement_latex="", lean_statement="True")
    st = PS(steps=[ProofStep(id="s1", kind="affirmation", statement="x", source=[SourceRef(line_id="p1.L01", excerpt="x")])],
            scopes=[], pattern={"kind": "aucun"})
    fm = Formalization(scopes=[], steps=[StepFormal(step_id="s1", role="prop", claim="1 = 1")])
    lean = LeanReport(file="", steps=[StepCheck(step_id="s1", decl="s1", status="non_verifie")])

    class E:
        def structured(self, *a, **kw):
            raise EngineError("réponse tronquée (max_tokens)")

    monkeypatch.setattr(agents, "_engine", lambda cfg, spec=None: E())
    out = agents.tier2_attempts(ref, st, fm, lean, PipelineConfig(reasoning_engine="x"))
    assert out.of("s1").agent_proof is None and out.of("s1").agent_refutation is None


def test_chain_link_is_checked_without_previous_link():
    from leanonsteroids.schemas import Formalization, ProofStep, ProofStructure as PS, SourceRef, StepFormal
    steps = [ProofStep(id="s2", kind="hypothese", statement="S = T", source=[SourceRef(line_id="l", excerpt="x")]),
             ProofStep(id="s5", kind="calcul", statement="A = B", depends_on=["s2"], source=[SourceRef(line_id="l", excerpt="x")]),
             ProofStep(id="s6", kind="calcul", statement="B = C", depends_on=["s5"], source=[SourceRef(line_id="l", excerpt="x")])]
    st = PS(steps=steps, scopes=[], pattern={"kind": "aucun"})
    fm = Formalization(scopes=[], steps=[StepFormal(step_id="s2", role="hyp", claim="1 = 1"),
                                         StepFormal(step_id="s5", role="prop", claim="1 = 1", uses=["s2"]),
                                         StepFormal(step_id="s6", role="prop", claim="1 = 1", uses=["s5"])])
    out = agents.independent_chain_links(st, fm)
    assert out.of("s6").uses == [] and out.of("s5").uses == ["s2"]  # l'hypothèse de récurrence reste


def test_tier2_slow_attempt_is_abandoned(monkeypatch):
    import time as _t
    from leanonsteroids.schemas import (Formalization, LeanReport, ProofStep, ProofStructure as PS, ReferenceStatement,
                                 SourceRef, StepCheck, StepFormal)
    ref = ReferenceStatement(exercise_id="t", statement_latex="", lean_statement="True")
    st = PS(steps=[ProofStep(id="s1", kind="affirmation", statement="x", source=[SourceRef(line_id="p1.L01", excerpt="x")])],
            scopes=[], pattern={"kind": "aucun"})
    fm = Formalization(scopes=[], steps=[StepFormal(step_id="s1", role="prop", claim="1 = 1")])
    lean = LeanReport(file="", steps=[StepCheck(step_id="s1", decl="s1", status="non_verifie")])

    class Slow:
        def structured(self, *a, **kw):
            _t.sleep(1.0)
            return agents.WireTier2(proof="rfl")

    monkeypatch.setattr(agents, "_engine", lambda cfg, spec=None: Slow())
    t0 = _t.monotonic()
    out = agents.tier2_attempts(ref, st, fm, lean, PipelineConfig(reasoning_engine="x", tier2_timeout_s=0.2))
    assert _t.monotonic() - t0 < 0.8 and out.of("s1").agent_proof is None


def test_reading_doubt_reaches_next_chain_link():
    from leanonsteroids.schemas import ProofStep, ProofStructure as PS, SourceRef
    from leanonsteroids.stages.fidelity import _chain_successors
    mk = lambda i, stmt, line: ProofStep(id=i, kind="calcul", statement=stmt, source=[SourceRef(line_id=line, excerpt="x")])
    st = PS(steps=[mk("s6", r"A = (n+1)(\frac{n}{2}+2)", "L08"), mk("s7", r"(n+1)(\frac{n}{2}+2) = B", "L09"),
                   mk("s8", "B = C", "L10")], scopes=[], pattern={"kind": "aucun"})
    # le doute sur la ligne L08 touche s6, donc s7 (qui reprend son membre) ; pas s8 (membre lu en L09)
    assert [s.id for s in _chain_successors(st, [st.steps[0]])] == ["s7"]


def test_unfaithful_translation_is_redone_once_with_the_judge_remark(monkeypatch):
    from leanonsteroids.schemas import Formalization, ProofStep, ProofStructure as PS, ReferenceStatement, SourceRef, StepFormal

    ref = ReferenceStatement(exercise_id="x", statement_latex="", lean_statement="True")
    st = PS(scopes=[], pattern={"kind": "aucun"}, steps=[
        ProofStep(id=i, kind="affirmation", statement=i, source=[SourceRef(line_id="p1.L01", excerpt=i)])
        for i in ("s1", "s2")])
    fm = Formalization(scopes=[], assemblage_agent="exact Copie.s1", steps=[
        StepFormal(step_id="s1", role="prop", claim="∃ c : ℝ, c = 0", agent_proof="exact ⟨0, rfl⟩", cites=["tvi"]),
        StepFormal(step_id="s2", role="prop", claim="True", agent_proof="trivial")])
    seen = {}

    def fake_llm(ref_, st_, cfg, *, only=None, done=None):
        seen.update(only)
        return Formalization(scopes=[], steps=[
            StepFormal(step_id="s1", role="prop", claim="∃ c : ℝ, 0 < c ∧ c < 1 ∧ c = 0"),
            StepFormal(step_id="s2", role="prop", claim="True")], provenance="agent fake")

    monkeypatch.setattr(agents, "_formalize_llm", fake_llm)
    out, changed = agents.reformalize(ref, st, fm, {"s1": "omet « c entre 0 et 1 »", "s2": "rien"}, cfg=None)
    assert "omet « c entre 0 et 1 »" in seen["s1"]
    assert changed == ["s1"]  # s2 : même énoncé, rien ne change
    s1, s2 = out.of("s1"), out.of("s2")
    assert s1.claim.startswith("∃ c : ℝ, 0 < c") and s1.agent_proof is None and s1.cites == ["tvi"]
    assert s2.agent_proof == "trivial" and out.assemblage_agent is None
    assert fm.of("s1").agent_proof == "exact ⟨0, rfl⟩"  # l'original n'est pas modifié


@needs_lean
@pytest.mark.lean
def test_judge_runs_alongside_level2(tmp_path, fake):
    """Le juge tourne en parallèle du niveau 2 ; son résultat rejoint la fidélité avant le verdict."""
    cfg = PipelineConfig(workspace=ROOT / "lean_workspace", ocr_engines=["fake:A", "fake:B"],
                         reasoning_engine="fake:R", judge_engine="fake:J", cache_dir=tmp_path / "cache")
    r = run(EX / "exercice.json", [EX / "copie_p1.webp"], tmp_path / "out", cfg)
    assert r.verdict.verdict == Verdict.verified, r.verdict.blocking_issues


def test_profile_m_fills_only_missing_engines():
    import argparse
    from leanonsteroids.cli import PROFILS, _apply_profile
    a = argparse.Namespace(ocr=None, effort_ocr=None, raisonnement="openrouter:x/y", arbitre=None, niveau2=None,
                           secours=None, assemblage=None, juge=None, effort_juge=None)
    _apply_profile(a, PROFILS["M"])
    assert a.raisonnement == "openrouter:x/y"  # donné explicitement : conservé
    assert a.arbitre.endswith("claude-opus-5.5") and a.secours.endswith("gpt-6-luna-pro") and a.effort_juge == "medium"
    assert a.ocr == PROFILS["M"]["ocr"] and a.ocr is not PROFILS["M"]["ocr"]
