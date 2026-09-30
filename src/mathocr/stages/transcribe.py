"""Transcription multi-moteurs avec consensus et arbitrage.

1. Prétraitement : orientation EXIF, réduction de taille, contraste.
2. Chaque moteur OCR lit la page **à l'aveugle** (sans l'énoncé) : il ne peut
   pas « corriger » la copie à partir de ce qu'il faudrait démontrer.
3. Consensus : alignement des lignes (position + similarité), diff par jetons
   LaTeX entre moteurs ; chaque divergence devient une incertitude avec ses
   lectures concurrentes et les moteurs qui les soutiennent.
4. Arbitrage : les fragments disputés sont renvoyés, zoomés, à un moteur de
   raisonnement qui tranche sur la forme du tracé et déclare s'il a utilisé le
   contexte (`context_dependent`).
"""

from __future__ import annotations

import difflib
import hashlib
import io
import logging
import re
import time
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from .. import prompts
from ..llm.base import Engine, EngineError, ImagePart, make_engine
from ..schemas import (
    EngineRun,
    LineStatus,
    PageImage,
    Reading,
    ReferenceStatement,
    TranscribedLine,
    Transcription,
    Uncertainty,
)

log = logging.getLogger("mathocr.transcribe")
LLM_MAX_SIDE = 2000


# -- Schémas « fil » (sorties des moteurs) -------------------------------------------


class WireAlt(BaseModel):
    text: str
    probability: float


class WireSpan(BaseModel):
    span: str
    alternatives: list[WireAlt]
    reason: str
    context_based: bool = False


class WireLine(BaseModel):
    bbox: list[int] = Field(description="[x0, y0, x1, y1] normalisés 0-1000")
    text: str
    status: Literal["normal", "barré", "illisible", "hors_sujet"] = "normal"
    confidence: float
    uncertain: list[WireSpan] = Field(default_factory=list)


class WirePage(BaseModel):
    lines: list[WireLine]
    notes: str = ""


class WireDecision(BaseModel):
    id: str
    readings: list[WireAlt]
    reason: str
    context_based: bool


class WireDecisions(BaseModel):
    decisions: list[WireDecision]


# -- Images ---------------------------------------------------------------------------


def load_page(path: Path, page: int) -> tuple[PageImage, Image.Image]:
    raw = Path(path).read_bytes()
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    meta = PageImage(page=page, path=str(path), width=im.width, height=im.height,
                     sha256=hashlib.sha256(raw).hexdigest())
    return meta, im


def to_part(im: Image.Image, label: str, max_side: int = LLM_MAX_SIDE) -> ImagePart:
    im = im.copy()
    im.thumbnail((max_side, max_side))
    im = ImageOps.autocontrast(im, cutoff=1)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return ImagePart(buf.getvalue(), "image/png", label)


def crop_line(im: Image.Image, bbox: tuple[int, int, int, int], margin: float = 0.35) -> Image.Image:
    x0, y0, x1, y1 = bbox
    h = y1 - y0
    return im.crop((max(0, x0 - int(h * margin)), max(0, y0 - int(h * margin)),
                    min(im.width, x1 + int(h * margin)), min(im.height, y1 + int(h * margin))))


# -- Normalisation et jetons ------------------------------------------------------------

_NORM = [(r"\left", ""), (r"\right", ""), (r"\,", ""), (r"\;", ""), (r"\!", ""), (r"\displaystyle", ""),
         (r"\mathbb{N}", "ℕ"), (r"\N", "ℕ"), (r"\mathbb N", "ℕ"), (r"\cdot", "*"), (r"\times", "*"),
         (r"\leqslant", r"\leq"), (r"\geqslant", r"\geq"), (r"\le ", r"\leq "), (r"\ge ", r"\geq ")]
_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|[A-Za-zÀ-ÿ]+|\d|\S")


def normalize(text: str) -> str:
    """Forme canonique pour comparer des lectures : ignore les différences de pure notation."""
    for a, b in _NORM:
        text = text.replace(a, b)
    text = re.sub(r"([\^_])\{([A-Za-z0-9])\}", r"\1\2", text)  # n^{2} -> n^2
    text = re.sub(r"\s*([=+\-*/<>(),])\s*", r"\1", text)  # espaces autour des opérateurs
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _y_iou(a, b) -> float:
    inter = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = max(a[3], b[3]) - min(a[1], b[1])
    return inter / union if union > 0 else 0.0


# -- Consensus -----------------------------------------------------------------------------


def consensus(page: int, runs: dict[str, list[WireLine]], width: int, height: int,
              uid_start: int = 1) -> tuple[list[TranscribedLine], list[Uncertainty]]:
    engines = [e for e, ls in runs.items() if ls]
    if not engines:
        return [], []
    anchor = engines[0]
    others = engines[1:]
    used = {e: set() for e in others}
    lines: list[TranscribedLine] = []
    uncs: list[Uncertainty] = []
    uid = uid_start

    def px(b):
        return (int(b[0] * width / 1000), int(b[1] * height / 1000), int(b[2] * width / 1000), int(b[3] * height / 1000))

    for i, a in enumerate(sorted(runs[anchor], key=lambda x: (x.bbox[1], x.bbox[0]))):
        lid = f"p{page}.L{i + 1:02d}"
        group = {anchor: a}
        for e in others:
            best, best_s = None, 0.35
            for j, b in enumerate(runs[e]):
                if j in used[e]:
                    continue
                s = 0.6 * _y_iou(a.bbox, b.bbox) + 0.4 * fuzz.ratio(normalize(a.text), normalize(b.text)) / 100
                if s > best_s:
                    best, best_s = j, s
            if best is not None:
                used[e].add(best)
                group[e] = runs[e][best]
        n = len(engines)
        agree = sum(1 for g in group.values() if normalize(g.text) == normalize(a.text))
        conf = min(g.confidence for g in group.values()) * (agree / n if n > 1 else 1.0)
        if len(group) < n:
            conf *= 0.7
        lines.append(TranscribedLine(id=lid, page=page, bbox=px(a.bbox), text=a.text,
                                     status=LineStatus(a.status), confidence=round(conf, 3),
                                     engine_readings={e: g.text for e, g in group.items()}))
        # Divergences entre moteurs -> incertitudes
        spans: dict[tuple[int, int], dict[str, set[str]]] = {}
        ta = tokens(normalize(a.text))
        a_norm = normalize(a.text)
        for e, g in group.items():
            if e == anchor or normalize(g.text) == a_norm:
                continue
            tb = tokens(normalize(g.text))
            b_norm = normalize(g.text)
            sm = difflib.SequenceMatcher(a=[t[0] for t in ta], b=[t[0] for t in tb], autojunk=False)
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == "equal":
                    continue
                i1c, i2c = (i1, i2) if i2 > i1 else (max(0, i1 - 1), min(len(ta), i1 + 1))
                if i2c <= i1c:
                    continue
                s0, s1 = ta[i1c][1], ta[i2c - 1][2]
                a_seg = a_norm[s0:s1]
                if i2 > i1:
                    b_seg = b_norm[tb[j1][1]:tb[j2 - 1][2]] if j2 > j1 else ""
                else:  # insertion chez l'autre moteur : on l'attache au jeton voisin
                    ins = b_norm[tb[j1][1]:tb[j2 - 1][2]] if j2 > j1 else ""
                    b_seg = (a_seg + ins) if i1c < i1 else (ins + a_seg)
                spans.setdefault((s0, s1), {a_seg: {anchor}}).setdefault(b_seg, set()).add(e)
        for (s0, s1), readings in spans.items():
            a_seg = a_norm[s0:s1]
            if a_seg not in a.text:  # la normalisation a déplacé le fragment : on garde le texte normalisé
                lines[-1].text = a_norm
            for e, g in group.items():
                if normalize(g.text) == a_norm:
                    readings[a_seg].add(e)
            tot = sum(len(v) for v in readings.values())
            rs = sorted((Reading(text=t, support=sorted(v), score=round(len(v) / tot, 3)) for t, v in readings.items()),
                        key=lambda r: -r.score)
            uncs.append(Uncertainty(id=f"U{uid:02d}", line_id=lid, span=a_seg, readings=rs, chosen=rs[0].text,
                                    reason="lectures divergentes entre moteurs"))
            uid += 1
        # Incertitudes déclarées par les moteurs eux-mêmes
        for e, g in group.items():
            for sp in g.uncertain:
                if sp.span not in lines[-1].text or len(sp.alternatives) < 2:
                    continue
                if any(u.line_id == lid and u.span == sp.span for u in uncs):
                    u = next(u for u in uncs if u.line_id == lid and u.span == sp.span)
                    for alt in sp.alternatives:
                        if all(r.text != alt.text for r in u.readings):
                            u.readings.append(Reading(text=alt.text, support=[e], score=round(alt.probability / len(engines), 3)))
                    u.context_dependent |= sp.context_based
                    continue
                rs = [Reading(text=x.text, support=[e], score=round(x.probability, 3)) for x in sp.alternatives]
                chosen = sp.span if any(r.text == sp.span for r in rs) else rs[0].text
                rs.sort(key=lambda r: (r.text != chosen, -r.score))
                uncs.append(Uncertainty(id=f"U{uid:02d}", line_id=lid, span=sp.span, readings=rs, chosen=chosen,
                                        reason=f"{e} : {sp.reason}", context_dependent=sp.context_based))
                uid += 1
    # Lignes vues par d'autres moteurs mais pas par l'ancre : contenu possiblement manqué.
    k = len(lines)
    for e in others:
        for j, b in enumerate(runs[e]):
            if j in used[e] or b.status != "normal":
                continue
            k += 1
            lines.append(TranscribedLine(id=f"p{page}.L{k:02d}", page=page, bbox=px(b.bbox), text=b.text,
                                         status=LineStatus.normal, confidence=round(b.confidence * 0.4, 3),
                                         engine_readings={e: b.text}))
    lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    return lines, uncs


# -- Arbitrage ------------------------------------------------------------------------------


def adjudicate(engine: Engine, ref: ReferenceStatement, page_img: Image.Image,
               lines: list[TranscribedLine], uncs: list[Uncertainty]) -> None:
    disputed = [u for u in uncs if len(u.readings) > 1 and u.readings[0].score < 0.999]
    if not disputed:
        return
    by_line = {ln.id: ln for ln in lines}
    images = [to_part(page_img, "page entière")]
    items = []
    for u in disputed:
        ln = by_line[u.line_id]
        images.append(to_part(crop_line(page_img, ln.bbox), f"{u.id} — ligne {u.line_id}", max_side=1600))
        items.append(f"- {u.id} (ligne {u.line_id} : « {ln.text} ») fragment « {u.span} » ; lectures : "
                     + " | ".join(f"« {r.text} »" for r in u.readings))
    out = engine.structured(prompts.ADJUDICATE_SYSTEM,
                            prompts.ADJUDICATE_TASK.format(statement=ref.statement_latex, items="\n".join(items)),
                            images, WireDecisions)
    for d in out.decisions:
        u = next((x for x in disputed if x.id == d.id), None)
        if u is None or not d.readings:
            continue
        known = {r.text: r for r in u.readings}
        new = []
        for alt in d.readings:
            r = known.get(alt.text) or Reading(text=alt.text, support=[])
            new.append(Reading(text=r.text, support=sorted(set(r.support) | {f"arbitre:{engine.name}"}),
                               score=round(alt.probability, 3)))
        u.readings = sorted(new, key=lambda r: -r.score)
        u.reason += f" ; arbitrage : {d.reason}"
        u.context_dependent = u.context_dependent or d.context_based
        if u.readings[0].text != u.chosen:
            ln = by_line[u.line_id]
            ln.text = ln.text.replace(u.span, u.readings[0].text, 1)
            u.chosen = u.span = u.readings[0].text


# -- Point d'entrée ----------------------------------------------------------------------------


def transcribe(images: list[Path], ref: ReferenceStatement, cfg) -> Transcription:
    if not cfg.ocr_engines:
        raise EngineError("aucun moteur OCR configuré : fournir --transcription ou --ocr anthropic:… etc.")
    cache = Path(getattr(cfg, "cache_dir", "runs/.cache"))
    pages, all_lines, all_uncs, runs_meta = [], [], [], []
    for pno, path in enumerate(images, start=1):
        meta, im = load_page(path, pno)
        pages.append(meta)
        part = to_part(im, f"page {pno}")
        runs: dict[str, list[WireLine]] = {}
        for spec in cfg.ocr_engines:
            t0 = time.monotonic()
            try:
                if spec.startswith("mathpix"):
                    eng = make_engine(spec)
                    scale = min(1.0, 4000 / max(im.size))
                    rw, rh = im.width * scale, im.height * scale
                    raw = eng.ocr_lines(to_part(im, f"page {pno}", max_side=4000))
                    runs[eng.name] = [WireLine(bbox=[int(b["bbox"][0] * 1000 / rw), int(b["bbox"][1] * 1000 / rh),
                                                     int(b["bbox"][2] * 1000 / rw), int(b["bbox"][3] * 1000 / rh)],
                                               text=b["text"], confidence=b["confidence"]) for b in raw]
                else:
                    eng = make_engine(spec, cache)
                    runs[eng.name] = eng.structured(prompts.OCR_SYSTEM, prompts.OCR_TASK, [part], WirePage).lines
                runs_meta.append(EngineRun(engine=eng.name, model=eng.model, mode="aveugle", ok=True,
                                           seconds=round(time.monotonic() - t0, 1)))
            except Exception as ex:  # noqa: BLE001 - un moteur défaillant ne doit pas arrêter les autres
                log.warning("moteur %s indisponible : %s", spec, ex)
                runs_meta.append(EngineRun(engine=spec, model="", mode="aveugle", ok=False, error=str(ex)))
        if not any(runs.values()):
            raise EngineError("aucun moteur OCR n'a répondu")
        lines, uncs = consensus(pno, runs, im.width, im.height, uid_start=len(all_uncs) + 1)
        if cfg.reasoning_engine:
            try:
                adjudicate(make_engine(cfg.reasoning_engine, cache), ref, im, lines, uncs)
                runs_meta.append(EngineRun(engine=cfg.reasoning_engine, model="", mode="arbitrage", ok=True))
            except Exception as ex:  # noqa: BLE001
                runs_meta.append(EngineRun(engine=cfg.reasoning_engine, model="", mode="arbitrage", ok=False, error=str(ex)))
        all_lines += lines
        all_uncs += uncs
    ok = [r.engine for r in runs_meta if r.ok and r.mode == "aveugle"]
    return Transcription(pages=pages, lines=all_lines, uncertainties=all_uncs, engines=runs_meta,
                         provenance=f"Consensus de {len(ok)} moteur(s) en lecture aveugle ({', '.join(ok)})"
                                    + (f", arbitrage par {cfg.reasoning_engine}" if cfg.reasoning_engine else "") + ".")
