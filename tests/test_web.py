"""Énoncé lu sur une capture (formalisation contrôlée) et interface web."""

import io
import json
import threading
import time
import urllib.request
import uuid

import pytest
from PIL import Image

from conftest import EX, ROOT, needs_lean
from leanonsteroids.llm.base import Engine
from leanonsteroids.pipeline import PipelineConfig
from leanonsteroids.stages import exercise_from_image as exi
from leanonsteroids.stages.exercise_from_image import WireCheck, WireExercise, build_reference

GOOD = {"titre": "Carré constant", "statement_latex": "Soit $f$ continue telle que $f(x)^2 = a$. Montrer que $f$ est constante.",
        "objets": [{"nom": "f", "type": "ℝ → ℝ", "latex": "f"}, {"nom": "a", "type": "ℝ", "latex": "a"}],
        "hypotheses": [{"nom": "h_cont", "lean": "Continuous f", "latex": "f continue"},
                       {"nom": "h_eq", "lean": "∀ x, f x ^ 2 = a", "latex": "f(x)^2 = a"}],
        "but_lean": "∀ x y, f x = f y", "lean_imports": ["Mathlib.Topology.Order.IntermediateValue", "Mathlib.Topology.Instances.Real.Lemmas"], "lean_opens": [], "remarques": ""}


def test_reference_has_the_context_form():
    ref = build_reference(WireExercise(**GOOD), "auto_x")
    assert ref.lean_statement == "∀ (f : ℝ → ℝ) (a : ℝ), (Continuous f) → (∀ x, f x ^ 2 = a) → (∀ x y, f x = f y)"
    assert [o.nom for o in ref.contexte.objets] == ["f", "a"] and ref.contexte.hypotheses[0].nom == "h_cont"
    assert ref.lean_imports[0] == "MathOCRCheck.Prelude"


class FakeEngine(Engine):
    def __init__(self, name, answers):
        self.name, self.model, self.answers = name, "simulé", answers

    def structured(self, system, text, images, schema, *, effort="high"):
        return self.answers[schema.__name__].pop(0)


@needs_lean
@pytest.mark.lean
@pytest.mark.parametrize("fidele", [True, False])
def test_statement_is_repaired_by_lean_then_checked_by_the_judge(tmp_path, monkeypatch, fidele):
    bad = dict(GOOD, but_lean="∀ x y, f x = g y")  # g inconnu : Lean refuse, le formalisateur corrige
    answers = {"WireExercise": [WireExercise(**bad), WireExercise(**GOOD)],
               "WireCheck": [WireCheck(fidele=fidele, explication="test")]}
    monkeypatch.setattr(exi, "make_engine", lambda spec, cache=None: FakeEngine(spec, answers))
    cfg = PipelineConfig(workspace=ROOT / "lean_workspace", reasoning_engine="fake:R", judge_engine="fake:J",
                         cache_dir=tmp_path / "cache")
    ref = exi.prepare_exercise([EX / "copie_p1.webp"], cfg, out_dir=tmp_path)
    assert not answers["WireExercise"]  # une reprise après l'erreur Lean
    assert "∀ x y, f x = f y" in ref.lean_statement
    assert bool(ref.validated_by) is fidele  # juge en désaccord : énoncé non validé
    saved = json.loads((tmp_path / f"{ref.exercise_id}.json").read_text())
    assert saved["titre"] == "Carré constant"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def _multipart(fields: dict, files: list[tuple[str, bytes]]):
    b = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, data in files:
        out += (f'--{b}\r\nContent-Disposition: form-data; name="images"; filename="{name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def test_upload_rules(tmp_path):
    from leanonsteroids.web.server import UserError, parse_multipart, save_images
    body, ctype = _multipart({"exercice": "mw06"}, [("a.png", _png())])
    fields, files = parse_multipart(ctype, body)
    assert fields == {"exercice": "mw06"} and len(files) == 1
    assert [p.name for p in save_images(files, tmp_path)] == ["page1.png"]
    with pytest.raises(UserError):
        save_images([("x.png", b"pas une image")], tmp_path)
    with pytest.raises(UserError):
        save_images([], tmp_path)


def test_web_flow(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer

    from leanonsteroids.schemas import ReferenceStatement
    from leanonsteroids.web import server

    def fake_prepare(images, cfg, out_dir=None):
        ref = ReferenceStatement(exercise_id="auto_test", statement_latex="Montrer que $1+1=2$.",
                                 lean_statement="1 + 1 = 2", validated_by="test")
        (out_dir / "auto_test.json").write_text(json.dumps({**ref.model_dump(), "titre": "Test"}))
        return ref
    monkeypatch.setattr(exi, "prepare_exercise", fake_prepare)
    app = server.App(tmp_path, lambda: PipelineConfig())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert b"Lean On Steroids" in urllib.request.urlopen(base + "/").read()
        ids = [e["id"] for e in json.loads(urllib.request.urlopen(base + "/api/exercices").read())["exercices"]]
        assert "mw06" in ids
        body, ctype = _multipart({}, [("enonce.png", _png())])
        req = urllib.request.Request(base + "/api/enonce", data=body, headers={"Content-Type": ctype})
        job = json.loads(urllib.request.urlopen(req).read())
        for _ in range(50):
            d = json.loads(urllib.request.urlopen(f"{base}/api/job/{job['id']}").read())
            if d["status"] != "en_cours":
                break
            time.sleep(0.1)
        assert d["status"] == "termine" and d["result"]["id"] == "auto_test" and d["result"]["valide"]
        assert "auto_test" in [e["id"] for e in app.exercises()]
        # Exercice inconnu : refus clair, pas de correction lancée
        body, ctype = _multipart({"exercice": "../../etc"}, [("p.png", _png())])
        req = urllib.request.Request(base + "/api/correction", data=body, headers={"Content-Type": ctype})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400
    finally:
        httpd.shutdown()
