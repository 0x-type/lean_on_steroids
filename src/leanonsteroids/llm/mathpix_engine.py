"""OCR spécialisé Mathpix (API v3/text) : lignes, contours et confiance par ligne.

Mathpix n'interprète pas : il transcrit. C'est précisément ce qui en fait un
bon contre-témoin face aux LLM de vision, qui peuvent « corriger » une copie
en s'appuyant sur le sens. Nécessite MATHPIX_APP_ID et MATHPIX_APP_KEY.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request

from .base import Engine, EngineError, ImagePart


class MathpixEngine(Engine):
    URL = "https://api.mathpix.com/v3/text"

    def __init__(self):
        self.app_id = os.environ.get("MATHPIX_APP_ID")
        self.app_key = os.environ.get("MATHPIX_APP_KEY")
        if not (self.app_id and self.app_key):
            raise EngineError("mathpix : MATHPIX_APP_ID / MATHPIX_APP_KEY absents")
        self.name = "mathpix"
        self.model = "v3/text"

    def structured(self, system, text, images, schema, *, effort="high"):
        raise EngineError("mathpix ne produit pas de sortie structurée libre ; utiliser ocr_lines()")

    def ocr_lines(self, image: ImagePart) -> list[dict]:
        """Retourne [{bbox: [x0,y0,x1,y1] en pixels, text, confidence}]."""
        body = json.dumps({
            "src": f"data:{image.mime};base64,{base64.b64encode(image.data).decode()}",
            "formats": ["text"],
            "include_line_data": True,
            "math_inline_delimiters": ["$", "$"],
            "rm_spaces": True,
        }).encode()
        req = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "app_id": self.app_id, "app_key": self.app_key, "Content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except Exception as ex:  # noqa: BLE001
            raise EngineError(f"mathpix : {ex}") from ex
        if "error" in data:
            raise EngineError(f"mathpix : {data['error']}")
        out = []
        for ln in data.get("line_data", []):
            cnt = ln.get("cnt") or []
            if not cnt or ln.get("included") is False:
                continue
            xs = [p[0] for p in cnt]
            ys = [p[1] for p in cnt]
            out.append({"bbox": [min(xs), min(ys), max(xs), max(ys)], "text": ln.get("text", ""),
                        "confidence": float(ln.get("confidence", ln.get("confidence_rate", 0.5)) or 0.5)})
        return out
