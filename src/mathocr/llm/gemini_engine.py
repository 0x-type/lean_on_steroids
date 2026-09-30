"""Moteur Gemini (SDK `google-genai`), sorties JSON contraintes par schéma Pydantic."""

from __future__ import annotations

from .base import Engine, EngineError, ImagePart


class GeminiEngine(Engine):
    def __init__(self, model: str):
        from google import genai  # dépendance optionnelle
        from google.genai import types

        self.types = types
        self.client = genai.Client()
        self.model = model
        self.name = f"gemini:{model}"

    def structured(self, system, text, images, schema, *, effort="high"):
        t = self.types
        parts: list = []
        for im in images:
            if im.label:
                parts.append(f"[Image : {im.label}]")
            parts.append(t.Part.from_bytes(data=im.data, mime_type=im.mime))
        parts.append(text)
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=parts,
                config=t.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                    media_resolution="MEDIA_RESOLUTION_HIGH",
                    temperature=0.0,
                ),
            )
        except Exception as ex:  # noqa: BLE001 - le SDK lève des classes variées selon le transport
            raise EngineError(f"{self.name} : {type(ex).__name__} {str(ex)[:200]}") from ex
        parsed = getattr(resp, "parsed", None)
        if parsed is None:
            try:
                return schema.model_validate_json(resp.text)
            except Exception as ex:  # noqa: BLE001
                raise EngineError(f"{self.name} : sortie JSON invalide") from ex
        return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
