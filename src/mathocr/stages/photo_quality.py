"""Contrôle local de la photo, avant tout appel payant.

Une photo floue, trop sombre, surexposée, minuscule ou vide finirait en
« examen nécessaire » : autant le dire tout de suite à l'élève, gratuitement.
Mesures (Pillow uniquement, < 1 s) :

* résolution (petit côté) ;
* contraste de l'encre : écart entre l'encre et le fond local ;
* proportion d'encre (page vide, ou page noire) ;
* netteté des traits : énergie du laplacien mesurée *sur l'encre seulement*
  (le papier lisse ne doit pas faire croire à du flou) ;
* surexposition (pixels saturés).

Les seuils sont calibrés sur la photo d'exemple et ses versions dégradées
(voir tests/test_photo_quality.py) ; ils restent volontairement prudents : on
ne refuse que ce qui est clairement inutilisable.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat
from pydantic import BaseModel, Field

MIN_SIDE = 800
MIN_INK = 0.002  # 0,2 % de pixels d'encre
MAX_INK = 0.35
MIN_SHARPNESS = 10.0  # original ≈ 53, flou léger ≈ 15, flou fort ≈ 2-5
MAX_SATURATED = 0.35
MIN_MEDIAN = 60
INK_DELTA = 40


class PhotoQuality(BaseModel):
    path: str
    ok: bool
    problems: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class PhotoRejected(Exception):
    def __init__(self, reports: list[PhotoQuality]):
        self.reports = reports
        msgs = [f"{Path(r.path).name} : {p}" for r in reports for p in r.problems]
        super().__init__("Photo refusée avant correction (aucun coût engagé) :\n- " + "\n- ".join(msgs))


def _hist_fraction(img: Image.Image, threshold: int) -> float:
    h = img.histogram()
    total = sum(h) or 1
    return sum(h[threshold:]) / total


def measure(data: bytes | Path) -> dict[str, float]:
    raw = Path(data).read_bytes() if isinstance(data, Path) else data
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("L")
    min_side = min(im.size)
    g = im.copy()
    g.thumbnail((1600, 1600))
    bg = g.filter(ImageFilter.BoxBlur(15))
    darker = ImageChops.subtract(bg, g)  # encre = plus sombre que le fond local
    ink_mask = darker.point(lambda v: 255 if v > INK_DELTA else 0)
    ink = _hist_fraction(ink_mask, 128)
    lap = g.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    edge = ImageChops.difference(lap, Image.new("L", lap.size, 128))
    sharp = ImageStat.Stat(edge, mask=ink_mask).mean[0] if ink > 0 else 0.0
    st = ImageStat.Stat(g)
    hist = g.histogram()
    total = sum(hist)
    acc, median = 0, 0
    for v, c in enumerate(hist):
        acc += c
        if acc >= total / 2:
            median = v
            break
    return {"petit_cote": float(min_side), "encre": round(ink, 4), "nettete": round(sharp, 1),
            "mediane": float(median), "saturation": round(_hist_fraction(g, 251), 3),
            "ecart_type": round(st.stddev[0], 1)}


def check(path: Path) -> PhotoQuality:
    m = measure(Path(path))
    problems = []
    if m["petit_cote"] < MIN_SIDE:
        problems.append(f"résolution trop faible ({int(m['petit_cote'])} px) : rapproche-toi ou photographie en pleine résolution")
    if m["mediane"] < MIN_MEDIAN:
        problems.append("photo trop sombre : éclaire la feuille")
    if m["saturation"] > MAX_SATURATED:
        problems.append("photo surexposée (reflet ou flash) : évite la lumière directe")
    if m["encre"] < MIN_INK:
        problems.append("aucune écriture détectée : la page semble vide ou trop pâle")
    elif m["encre"] > MAX_INK:
        problems.append("trop de zones sombres : cadre uniquement la feuille")
    elif m["nettete"] < MIN_SHARPNESS:
        problems.append("photo floue : stabilise le téléphone et fais la mise au point sur l'écriture")
    return PhotoQuality(path=str(path), ok=not problems, problems=problems, metrics=m)
