"""Moteur OpenRouter testé sans réseau : on intercepte l'appel HTTP de l'API compatible OpenAI."""

import json

import pytest
from openai.types.chat import ChatCompletion

from mathocr.llm.base import CachedEngine, ImagePart, make_engine
from mathocr.llm.pricing import ledger_scope
from mathocr.stages.transcribe import WirePage

PAGE = {"lines": [{"bbox": [1, 2, 3, 4], "text": "$0^2 = 0$", "status": "normal", "confidence": 0.9,
                   "uncertain": []}], "notes": ""}


def completion(content, cost=0.0123, finish="stop"):
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500, "cost": cost}})


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    eng = make_engine("openrouter:google/gemini-test")
    calls = []

    def fake_create(**kw):
        calls.append(kw)
        return eng._replies.pop(0)

    monkeypatch.setattr(eng.client.chat.completions, "create", fake_create)
    eng._calls = calls
    return eng


def test_request_shape_and_reported_cost(engine, tmp_path):
    engine._replies = [completion(json.dumps(PAGE))]
    cached = CachedEngine(engine, tmp_path)
    with ledger_scope() as led:
        out = cached.structured("sys", "lis", [ImagePart(b"PNG", "image/png", "page 1")], WirePage)
        cached.structured("sys", "lis", [ImagePart(b"PNG", "image/png", "page 1")], WirePage)  # cache
        rep = led.report()
    assert out.lines[0].text == "$0^2 = 0$"
    kw = engine._calls[0]
    assert kw["model"] == "google/gemini-test"
    assert kw["response_format"]["type"] == "json_schema" and kw["response_format"]["json_schema"]["strict"]
    assert kw["extra_body"]["provider"] == {"require_parameters": True}
    assert kw["extra_body"]["usage"] == {"include": True}
    img = [p for p in kw["messages"][1]["content"] if p["type"] == "image_url"][0]
    assert img["image_url"]["url"].startswith("data:image/png;base64,")
    assert rep.entries[0].cost_usd == 0.0123 and rep.entries[1].cached and rep.total_usd == 0.0123
    assert rep.complete and len(engine._calls) == 1


def test_fenced_json_and_one_repair(engine):
    engine._replies = [completion("pas du json"), completion("```json\n" + json.dumps(PAGE) + "\n```", cost=0.001)]
    out = engine.structured("sys", "lis", [], WirePage)
    assert out.lines[0].confidence == 0.9
    assert len(engine._calls) == 2 and engine.last_usage["cost_usd"] == pytest.approx(0.0133)


def test_falls_back_to_json_mode_when_no_strict_provider(engine, monkeypatch):
    import httpx
    import openai

    def not_found(**kw):
        engine._calls.append(kw)
        if kw["response_format"]["type"] == "json_schema":
            raise openai.NotFoundError("No endpoints found that support the requested parameters",
                                       response=httpx.Response(404, request=httpx.Request("POST", "http://x")),
                                       body=None)
        return completion(json.dumps(PAGE))

    monkeypatch.setattr(engine.client.chat.completions, "create", not_found)
    out = engine.structured("sys", "lis", [], WirePage)
    assert out.lines and [c["response_format"]["type"] for c in engine._calls] == ["json_schema", "json_object"]
    assert "schéma" in engine._calls[1]["messages"][0]["content"]
