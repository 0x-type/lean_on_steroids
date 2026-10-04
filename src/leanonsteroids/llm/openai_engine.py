"""Moteur OpenAI (API Responses, sorties structurées Pydantic)."""

from __future__ import annotations

import base64

from .base import Engine, EngineError, ImagePart


class OpenAIEngine(Engine):
    def __init__(self, model: str):
        import openai  # dépendance optionnelle

        self._openai = openai
        self.client = openai.OpenAI()
        self.model = model
        self.name = f"openai:{model}"

    def structured(self, system, text, images, schema, *, effort="high"):
        content: list[dict] = []
        for im in images:
            if im.label:
                content.append({"type": "input_text", "text": f"[Image : {im.label}]"})
            content.append({"type": "input_image", "detail": "high",
                            "image_url": f"data:{im.mime};base64,{base64.b64encode(im.data).decode()}"})
        content.append({"type": "input_text", "text": text})
        o = self._openai
        try:
            resp = self.client.responses.parse(
                model=self.model,
                instructions=system,
                input=[{"role": "user", "content": content}],
                text_format=schema,
                reasoning={"effort": {"max": "high", "xhigh": "high"}.get(effort, effort)},
            )
        except o.RateLimitError as ex:
            raise EngineError(f"{self.name} : limite de débit") from ex
        except o.APIStatusError as ex:
            raise EngineError(f"{self.name} : erreur API {ex.status_code}") from ex
        except o.APIConnectionError as ex:
            raise EngineError(f"{self.name} : connexion impossible") from ex
        u = getattr(resp, "usage", None)
        if u is not None:
            cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
            self.last_usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                               "cache_read_tokens": cached}
        if resp.output_parsed is None:
            raise EngineError(f"{self.name} : sortie structurée absente")
        return resp.output_parsed
