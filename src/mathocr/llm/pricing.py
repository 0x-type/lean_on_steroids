"""Compteur de coûts : jetons réellement consommés par chaque appel, et leur prix.

Les prix Claude sont ceux de la grille publique d'Anthropic (USD par million
de jetons). Pour les autres fournisseurs, les prix changent souvent et ne sont
pas codés en dur : les fournir par variable d'environnement, par exemple

    MATHOCR_PRICE_gemini-3-pro="1.25,10"      # entrée, sortie ($ / M jetons)
    MATHOCR_PRICE_mathpix="0.004"             # $ par image

Avec OpenRouter, le coût exact facturé est renvoyé par l'API et utilisé tel quel.
Un appel sans prix connu est compté, signalé, mais jamais deviné.
"""

from __future__ import annotations

import contextlib
import contextvars
import os

from ..schemas import CostReport, UsageEntry

# (entrée, sortie, lecture de cache) en $ par million de jetons
ANTHROPIC_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def price_for(engine: str, model: str) -> tuple[float, float, float] | None:
    env = os.environ.get(f"MATHOCR_PRICE_{model}") or os.environ.get(f"MATHOCR_PRICE_{engine}")
    if env:
        parts = [float(x) for x in env.split(",")]
        if len(parts) == 1:
            return (parts[0], 0.0, 0.0)  # prix à l'unité (image)
        return (parts[0], parts[1], parts[2] if len(parts) > 2 else parts[0])
    if engine.startswith("anthropic:"):
        return ANTHROPIC_PRICES.get(model)
    return None


def cost_of(e: UsageEntry) -> float | None:
    if e.cached:
        return 0.0
    p = price_for(e.engine, e.model)
    if p is None:
        return None
    if e.images and not e.input_tokens and not e.output_tokens:
        return round(p[0] * e.images, 6)
    uncached = max(0, e.input_tokens - e.cache_read_tokens)
    return round((uncached * p[0] + e.output_tokens * p[1] + e.cache_read_tokens * p[2]) / 1e6, 6)


class Ledger:
    def __init__(self) -> None:
        self.entries: list[UsageEntry] = []
        self.stage = "?"

    def add(self, **kw) -> UsageEntry:
        e = UsageEntry(stage=_stage.get() or self.stage, **kw)
        if e.cached:
            e.cost_usd = 0.0
        elif e.cost_usd is None:  # coût non fourni par le fournisseur (OpenRouter le fournit)
            e.cost_usd = cost_of(e)
        self.entries.append(e)
        return e

    def report(self, lean_seconds: float = 0.0) -> CostReport:
        known = [e.cost_usd for e in self.entries if e.cost_usd is not None]
        return CostReport(entries=self.entries, total_usd=round(sum(known), 4),
                          complete=all(e.cost_usd is not None for e in self.entries),
                          llm_calls=sum(1 for e in self.entries if not e.cached),
                          cached_calls=sum(1 for e in self.entries if e.cached), lean_seconds=lean_seconds)


_current: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar("mathocr_ledger", default=None)
# Étape en cours, propre à chaque fil d'exécution (le juge tourne en parallèle du niveau 2).
_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar("mathocr_stage", default=None)


@contextlib.contextmanager
def ledger_scope():
    led = Ledger()
    tok = _current.set(led)
    try:
        yield led
    finally:
        _current.reset(tok)


def record(**kw) -> None:
    led = _current.get()
    if led is not None:
        led.add(**kw)


def set_stage(stage: str) -> None:
    led = _current.get()
    if led is not None:
        led.stage = stage
    _stage.set(stage)
