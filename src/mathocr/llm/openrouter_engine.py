"""Moteur OpenRouter : une seule clé (OPENROUTER_API_KEY) pour Claude, Gemini, GPT, Qwen…

API compatible OpenAI (`/api/v1/chat/completions`) :
* images en `image_url` (data URI base64) ;
* sortie contrainte par schéma JSON strict (`response_format: json_schema`), avec
  `provider.require_parameters = true` pour n'être routé que vers des fournisseurs
  qui respectent vraiment le schéma (sinon il serait ignoré en silence) ;
* `reasoning.effort` unifié entre fournisseurs ;
* `usage.include = true` : OpenRouter renvoie le coût exact facturé (`usage.cost`, USD),
  que le compteur de coûts utilise tel quel.

Si aucun fournisseur du modèle ne prend en charge le schéma strict, repli sur le mode
JSON simple avec le schéma dans la consigne ; la sortie est de toute façon validée par
Pydantic, avec une relance unique en cas de JSON invalide.
"""

from __future__ import annotations

import base64
import json
import os
import re

from pydantic import ValidationError

from .base import Engine, EngineError, ImagePart

BASE_URL = "https://openrouter.ai/api/v1"


def _extract_json(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith(("{", "[")):
        i = t.find("{")
        t = t[i:] if i >= 0 else t
    return t


class OpenRouterEngine(Engine):
    def __init__(self, model: str, max_tokens: int = 32000):
        import openai  # SDK compatible : OpenRouter expose l'API OpenAI

        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise EngineError("openrouter : OPENROUTER_API_KEY absente")
        self._openai = openai
        self.client = openai.OpenAI(base_url=BASE_URL, api_key=key,
                                    default_headers={"X-Title": "MathOCR"})
        self.model = model
        self.name = f"openrouter:{model}"
        self.max_tokens = max_tokens
        self._strict_ok = True  # passe à False si aucun fournisseur n'accepte le schéma strict

    def _content(self, text: str, images: list[ImagePart]) -> list[dict]:
        parts: list[dict] = []
        for im in images:
            if im.label:
                parts.append({"type": "text", "text": f"[Image : {im.label}]"})
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{im.mime};base64,{base64.b64encode(im.data).decode()}"}})
        parts.append({"type": "text", "text": text})
        return parts

    def _call(self, messages: list[dict], schema, effort: str, strict: bool):
        from openai.lib._pydantic import to_strict_json_schema

        o = self._openai
        if strict:
            rf = {"type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True,
                                                         "schema": to_strict_json_schema(schema)}}
            provider = {"require_parameters": True}
        else:
            rf = {"type": "json_object"}
            provider = {}
        try:
            return self.client.chat.completions.create(
                model=self.model, messages=messages, response_format=rf, max_tokens=self.max_tokens,
                extra_body={"provider": provider, "reasoning": {"effort": effort}, "usage": {"include": True}},
            )
        except o.NotFoundError as ex:  # « No endpoints found that support the requested parameters »
            if strict:
                return None
            raise EngineError(f"{self.name} : modèle introuvable ({ex.message})") from ex
        except o.RateLimitError as ex:
            raise EngineError(f"{self.name} : limite de débit ou crédit épuisé") from ex
        except o.AuthenticationError as ex:
            raise EngineError(f"{self.name} : clé OpenRouter refusée") from ex
        except o.APIStatusError as ex:
            raise EngineError(f"{self.name} : erreur API {ex.status_code} ({ex.message})") from ex
        except o.APIConnectionError as ex:
            raise EngineError(f"{self.name} : connexion impossible à OpenRouter") from ex

    def _usage(self, resp) -> dict:
        u = getattr(resp, "usage", None)
        if u is None:
            return {}
        extra = u.model_extra or {}
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        out = {"input_tokens": u.prompt_tokens or 0, "output_tokens": u.completion_tokens or 0,
               "cache_read_tokens": cached}
        if extra.get("cost") is not None:
            out["cost_usd"] = float(extra["cost"])
        return out

    def structured(self, system, text, images, schema, *, effort="high"):
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": self._content(text, images)}]
        strict = self._strict_ok
        if not strict:
            messages[0]["content"] = system + "\n\nRéponds uniquement par un objet JSON conforme à ce schéma :\n" \
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        resp = self._call(messages, schema, effort, strict)
        if resp is None:  # schéma strict non pris en charge par ce modèle : mode JSON simple
            self._strict_ok = False
            return self.structured(system, text, images, schema, effort=effort)
        usage = self._usage(resp)
        content = resp.choices[0].message.content if resp.choices else ""
        if resp.choices and resp.choices[0].finish_reason == "length":
            self.last_usage = usage
            raise EngineError(f"{self.name} : réponse tronquée (max_tokens)")
        try:
            out = schema.model_validate_json(_extract_json(content))
        except ValidationError as ex:
            # Une seule relance, sur la forme uniquement.
            messages += [{"role": "assistant", "content": content or ""},
                         {"role": "user", "content": "Ta réponse n'est pas un JSON valide pour le schéma demandé : "
                          + str(ex)[:800] + "\nRenvoie uniquement le JSON corrigé."}]
            resp2 = self._call(messages, schema, effort, strict)
            if resp2 is None:
                raise EngineError(f"{self.name} : sortie JSON invalide") from ex
            u2 = self._usage(resp2)
            for k, v in u2.items():
                usage[k] = usage.get(k, 0) + v
            try:
                out = schema.model_validate_json(_extract_json(resp2.choices[0].message.content))
            except ValidationError as ex2:
                self.last_usage = usage
                raise EngineError(f"{self.name} : sortie JSON invalide après relance") from ex2
        self.last_usage = usage
        return out


def list_vision_models(timeout: float = 30) -> list[dict]:
    """Modèles OpenRouter qui acceptent des images, avec prix et prise en charge du schéma strict."""
    import urllib.request

    with urllib.request.urlopen(f"{BASE_URL}/models", timeout=timeout) as r:
        data = json.loads(r.read())["data"]
    out = []
    for m in data:
        if "image" not in (m.get("architecture", {}).get("input_modalities") or []):
            continue
        p = m.get("pricing", {})
        sp = m.get("supported_parameters") or []
        out.append({"id": m["id"], "in": float(p.get("prompt") or 0) * 1e6, "out": float(p.get("completion") or 0) * 1e6,
                    "structured": "structured_outputs" in sp, "reasoning": "reasoning" in sp,
                    "context": m.get("context_length")})
    return sorted(out, key=lambda x: x["id"])
