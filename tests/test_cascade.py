"""Mode cascade : deux lecteurs bon marché, relecture zoomée des seules lignes disputées."""

import json

from conftest import EX
from mathocr.llm.base import Engine
from mathocr.pipeline import PipelineConfig
from mathocr.schemas import ReferenceStatement
from mathocr.stages import transcribe as tmod
from mathocr.stages.transcribe import WireCropReadings, WirePage

TR = json.loads((EX / "fixtures" / "transcription.json").read_text())
REF = ReferenceStatement(**json.loads((EX / "exercice.json").read_text()))
L11 = next(ln for ln in TR["lines"] if ln["id"] == "p1.L11")


def page(overrides):
    out = []
    for ln in TR["lines"]:
        x0, y0, x1, y1 = ln["bbox"]
        out.append({"bbox": [x0 * 1000 // 1500, y0 * 1000 // 2000, x1 * 1000 // 1500, y1 * 1000 // 2000],
                    "text": overrides.get(ln["id"], ln["text"]), "status": ln["status"], "confidence": 0.9})
    return WirePage(lines=out)


class Fake(Engine):
    def __init__(self, name, log):
        self.name, self.model, self.log = name, "simulé", log

    def structured(self, system, text, images, schema, *, effort="high"):
        self.log.append((self.name, schema.__name__, [im.label for im in images]))
        if schema is WirePage:
            return page({"p1.L11": r"$= n^2 + (2n-1)$ (d'après H.R.)"} if self.name == "cheap:B" else {})
        if schema is WireCropReadings:
            return WireCropReadings(items=[{"id": im.label, "text": L11["text"], "confidence": 0.95}
                                           for im in images])
        raise AssertionError(schema)


def _run(monkeypatch, tmp_path, audit=0.0):
    log = []
    monkeypatch.setattr(tmod, "make_engine", lambda spec, cache=None, **kw: Fake(spec, log))
    cfg = PipelineConfig(ocr_engines=["cheap:A", "cheap:B"], ocr_mode="cascade", ocr_strong=["strong:S"],
                         audit_rate=audit, cache_dir=tmp_path)
    return tmod.transcribe([EX / "copie_p1.webp"], REF, cfg), log


def test_only_disputed_line_is_zoomed(monkeypatch, tmp_path):
    tr, log = _run(monkeypatch, tmp_path)
    strong_calls = [c for c in log if c[0] == "strong:S"]
    assert len(strong_calls) == 1  # un seul appel groupé
    (_, _, labels), = strong_calls
    l11 = next(ln for ln in tr.lines if "(2n" in ln.text and "H.R" in ln.text)
    assert labels == [l11.id]  # seule la ligne disputée est envoyée, zoomée
    assert "(2n+1)" in l11.text  # 2 lectures contre 1
    u = next(u for u in tr.uncertainties if u.line_id == l11.id)
    assert u.chosen == "+" and sorted(u.readings[0].support) == ["cheap:A", "strong:S"]


def test_audit_rereads_some_agreed_lines(monkeypatch, tmp_path):
    tr, log = _run(monkeypatch, tmp_path, audit=0.2)
    (_, _, labels), = [c for c in log if c[0] == "strong:S"]
    assert len(labels) > 1
    assert any(r.mode == "audit" for r in tr.engines)


def test_evaluate_command_end_to_end(monkeypatch, tmp_path, capsys):
    from mathocr import cli

    log = []
    monkeypatch.setattr(tmod, "make_engine", lambda spec, cache=None, **kw: Fake(spec, log))
    rc = cli.main(["evaluer", "--ocr", "cheap:A", "--ocr", "cheap:B", "--mode-ocr", "cascade",
                   "--ocr-fort", "strong:S", "--sortie", str(tmp_path / "ev"), "--cache", str(tmp_path / "c")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| somme_impairs_p1 |" in out and "cascade: cheap:A + cheap:B → strong:S" in out
    scores = json.loads((tmp_path / "ev" / "scores.json").read_text())
    assert scores[0]["silent_errors"] == 0 and not scores[0]["missed_lines"]
