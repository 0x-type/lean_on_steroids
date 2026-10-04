"""Interface commune aux moteurs (LLM de vision/raisonnement, OCR spécialisé).

Un moteur est désigné par une chaîne ``fournisseur:modèle`` :

    anthropic:claude-opus-5-5     openai:gpt-5        gemini:gemini-3-pro
    openrouter:<fournisseur>/<modèle>   (une seule clé : OPENROUTER_API_KEY)
    mathpix

Toutes les réponses structurées sont validées par Pydantic. Chaque appel est
mis en cache (clé = hachage du moteur, des consignes, du texte, des images et
du schéma) : une exécution peut être rejouée à l'identique, sans réseau, et
sert de trace d'audit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class ImagePart:
    data: bytes
    mime: str = "image/png"
    label: str = ""


class EngineError(RuntimeError):
    pass


class Engine:
    """Moteur capable de produire une sortie structurée à partir de texte et d'images."""

    name: str = "engine"
    model: str = ""
    # Jetons consommés par le dernier appel : {input_tokens, output_tokens, cache_read_tokens}
    last_usage: dict | None = None

    def structured(self, system: str, text: str, images: list[ImagePart], schema: type[T],
                   *, effort: str = "high") -> T:  # pragma: no cover - interface
        raise NotImplementedError


class CachedEngine(Engine):
    """Enveloppe : cache disque des réponses (enregistrement / rejeu)."""

    def __init__(self, inner: Engine, cache_dir: Path, *, replay_only: bool = False):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.cache_dir = cache_dir
        self.replay_only = replay_only
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, system: str, text: str, images: list[ImagePart], schema: type[BaseModel], effort: str) -> str:
        h = hashlib.sha256()
        for part in (self.name, self.model, system, text, schema.__name__,
                     json.dumps(schema.model_json_schema(), sort_keys=True), effort):
            h.update(part.encode())
            h.update(b"\0")
        for im in images:
            h.update(hashlib.sha256(im.data).digest())
        return h.hexdigest()[:32]

    def structured(self, system, text, images, schema, *, effort="high"):
        key = self._key(system, text, images, schema, effort)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.name)
        path = self.cache_dir / f"{safe}_{schema.__name__}_{key}.json"
        from .pricing import record

        if path.exists():
            data = json.loads(path.read_text())
            usage = data.get("usage") or {}
            record(engine=self.name, model=self.model, cached=True, **usage)
            return schema.model_validate_json(data["output"])
        if self.replay_only:
            raise EngineError(f"réponse absente du cache de rejeu : {path.name}")
        t0 = time.monotonic()
        self.inner.last_usage = None
        out = self.inner.structured(system, text, images, schema, effort=effort)
        usage = self.inner.last_usage or {}
        record(engine=self.name, model=self.model, cached=False, **usage)
        path.write_text(json.dumps({
            "engine": self.name, "model": self.model, "schema": schema.__name__,
            "seconds": round(time.monotonic() - t0, 2), "usage": usage, "system": system, "text": text,
            "images": [im.label or f"{len(im.data)} octets" for im in images],
            "output": out.model_dump_json(),
        }, ensure_ascii=False, indent=1))
        return out


def make_engine(spec: str, cache_dir: Path | None = None, *, replay_only: bool = False) -> Engine:
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    if provider == "anthropic":
        from .anthropic_engine import AnthropicEngine
        eng: Engine = AnthropicEngine(model or "claude-opus-5-5")
    elif provider == "openai":
        from .openai_engine import OpenAIEngine
        eng = OpenAIEngine(model or os.environ.get("MATHOCR_OPENAI_MODEL", "gpt-5"))
    elif provider in ("gemini", "google"):
        from .gemini_engine import GeminiEngine
        eng = GeminiEngine(model or os.environ.get("MATHOCR_GEMINI_MODEL", "gemini-3-pro"))
    elif provider == "openrouter":
        from .openrouter_engine import OpenRouterEngine
        if not model:
            raise EngineError("openrouter : préciser le modèle, ex. openrouter:google/<modèle> (voir « leanonsteroids modeles »)")
        eng = OpenRouterEngine(model)
    elif provider == "mathpix":
        from .mathpix_engine import MathpixEngine
        eng = MathpixEngine()
    else:
        raise EngineError(f"moteur inconnu : {spec}")
    if cache_dir is not None:
        return CachedEngine(eng, cache_dir, replay_only=replay_only)
    return eng
