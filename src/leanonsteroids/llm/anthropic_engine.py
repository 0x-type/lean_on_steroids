"""Moteur Claude (SDK officiel `anthropic`), sorties structurées validées par Pydantic."""

from __future__ import annotations

import base64

from .base import Engine, EngineError, ImagePart


class AnthropicEngine(Engine):
    def __init__(self, model: str = "claude-opus-5-5", max_tokens: int = 16000):
        import anthropic  # dépendance optionnelle

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.name = f"anthropic:{model}"
        self.max_tokens = max_tokens

    def structured(self, system, text, images, schema, *, effort="high"):
        content: list[dict] = []
        for im in images:
            if im.label:
                content.append({"type": "text", "text": f"[Image : {im.label}]"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": im.mime, "data": base64.standard_b64encode(im.data).decode()}})
        content.append({"type": "text", "text": text})
        a = self._anthropic
        try:
            # Repli côté serveur si un classifieur de sécurité refuse la requête.
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError as ex:
            raise EngineError(f"{self.name} : limite de débit ({ex.message})") from ex
        except a.APIStatusError as ex:
            raise EngineError(f"{self.name} : erreur API {ex.status_code} ({ex.message})") from ex
        except a.APIConnectionError as ex:
            raise EngineError(f"{self.name} : connexion impossible") from ex
        u = resp.usage
        self.last_usage = {"input_tokens": (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                           + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                           "output_tokens": u.output_tokens or 0,
                           "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        if resp.stop_reason == "refusal":
            raise EngineError(f"{self.name} : requête refusée ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise EngineError(f"{self.name} : réponse tronquée (max_tokens)")
        if resp.parsed_output is None:
            raise EngineError(f"{self.name} : sortie structurée absente")
        return resp.parsed_output
