from pydantic import BaseModel

from leanonsteroids.llm.base import CachedEngine, Engine
from leanonsteroids.llm.pricing import ledger_scope, set_stage


class Out(BaseModel):
    x: int


class FakeOpus(Engine):
    name, model = "anthropic:claude-opus-5-5", "claude-opus-5-5"

    def structured(self, system, text, images, schema, *, effort="high"):
        self.last_usage = {"input_tokens": 5000, "output_tokens": 6000, "cache_read_tokens": 0}
        return Out(x=1)


def test_real_cost_then_free_cache_hit(tmp_path):
    eng = CachedEngine(FakeOpus(), tmp_path)
    with ledger_scope() as led:
        set_stage("transcription")
        eng.structured("s", "t", [], Out)
        eng.structured("s", "t", [], Out)  # même requête : servie par le cache
        rep = led.report()
    first, second = rep.entries
    assert first.cost_usd == round((5000 * 4 + 6000 * 20) / 1e6, 6)  # 0,14 $
    assert second.cached and second.cost_usd == 0.0
    assert rep.total_usd == 0.14 and rep.llm_calls == 1 and rep.cached_calls == 1 and rep.complete


def test_unknown_price_is_flagged_not_guessed(tmp_path, monkeypatch):
    class G(FakeOpus):
        name, model = "gemini:gemini-x", "gemini-x"

    with ledger_scope() as led:
        CachedEngine(G(), tmp_path).structured("s", "t", [], Out)
        rep = led.report()
    assert rep.entries[0].cost_usd is None and not rep.complete

    monkeypatch.setenv("MATHOCR_PRICE_gemini-x", "1,10")
    with ledger_scope() as led:
        CachedEngine(G(), tmp_path / "b").structured("s", "t", [], Out)
        assert led.report().total_usd == round((5000 * 1 + 6000 * 10) / 1e6, 4)
