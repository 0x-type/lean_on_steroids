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


class WireCropReading(BaseModel):
    id: str
    text: str
    confidence: float
    uncertain: list[WireSpan] = Field(default_factory=list)


class WireCropReadings(BaseModel):
    items: list[WireCropReading]


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
    x0, x1 = sorted((bbox[0], bbox[2]))
    y0, y1 = sorted((bbox[1], bbox[3]))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return im
    h = y1 - y0
    return im.crop((max(0, x0 - int(h * margin)), max(0, y0 - int(h * margin)),
                    min(im.width, x1 + int(h * margin)), min(im.height, y1 + int(h * margin))))


# -- Normalisation et jetons ------------------------------------------------------------

_NORM = [(r"\left", ""), (r"\right", ""), (r"\,", ""), (r"\;", ""), (r"\!", ""), (r"\displaystyle", ""),
         (r"\mathbb{N}", "ℕ"), (r"\N", "ℕ"), (r"\mathbb N", "ℕ"), (r"\cdot", "*"), (r"\times", "*"),
         (r"\qquad", " "), (r"\quad", " "), (r"\leqslant", r"\leq"), (r"\geqslant", r"\geq"), (r"\le ", r"\leq "), (r"\ge ", r"\geq ")]
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


# -- Coordonnées des boîtes -------------------------------------------------------------------

def _box_ok(x0, y0, x1, y1) -> bool:
    return x0 < x1 and y0 < y1 and (x1 - x0) >= 0.5 * (y1 - y0)


def normalize_boxes(lines: list[WireLine]) -> tuple[list[WireLine], float]:
    """Ramène chaque boîte à [x0, y0, x1, y1] ; retourne aussi la part de boîtes conformes d'origine.

    Les consignes demandent [x0, y0, x1, y1], mais certains modèles renvoient leur convention native
    ([y0, x0, y1, x1]) ou mélangent les conventions d'une boîte à l'autre : on répare boîte par boîte
    (une ligne d'écriture est plus large que haute), sinon on trie les coordonnées."""
    out, conform = [], 0
    for ln in lines:
        b = ln.bbox
        if len(b) != 4:
            out.append(ln)
            continue
        a, c, d, e = b
        if _box_ok(a, c, d, e):
            conform += 1
            fixed = [a, c, d, e]
        elif _box_ok(c, a, d, e):  # [y0, x0, x1, y1]
            fixed = [c, a, d, e]
        elif _box_ok(c, a, e, d):  # [y0, x0, y1, x1]
            fixed = [c, a, e, d]
        else:
            fixed = [min(a, d), min(c, e), max(a, d), max(c, e)]
        out.append(ln.model_copy(update={"bbox": fixed}))
    return out, conform / max(1, len(lines))


def ink_map(im: Image.Image, side: int = 500) -> tuple[list[list[int]], int, int]:
    """Carte d'encre réduite (somme cumulée 2D) : pixel plus sombre que son voisinage."""
    from PIL import ImageFilter

    g = ImageOps.autocontrast(im.convert("L"), cutoff=1)
    g.thumbnail((side, side))
    bg = g.filter(ImageFilter.BoxBlur(max(4, side // 60)))
    w, h = g.size
    gp, bp = g.load(), bg.load()
    cum = [[0] * (w + 1) for _ in range(h + 1)]
    for y in range(h):
        row, prev, acc = cum[y + 1], cum[y], 0
        for x in range(w):
            acc += 1 if gp[x, y] < bp[x, y] - 18 else 0
            row[x + 1] = prev[x + 1] + acc
    return cum, w, h


def _ink_density(cum, w: int, h: int, b) -> float | None:
    """Part de pixels d'encre dans une boîte normalisée 0–1000 ; None si la boîte sort de la page."""
    x0, y0, x1, y1 = b
    if x1 <= x0 or y1 <= y0 or x0 < -20 or y0 < -20 or x1 > 1020 or y1 > 1020:
        return None
    X0, X1 = max(0, int(x0 * w / 1000)), min(w, max(int(x0 * w / 1000) + 1, int(x1 * w / 1000)))
    Y0, Y1 = max(0, int(y0 * h / 1000)), min(h, max(int(y0 * h / 1000) + 1, int(y1 * h / 1000)))
    area = (X1 - X0) * (Y1 - Y0)
    if area <= 0:
        return None
    return (cum[Y1][X1] - cum[Y0][X1] - cum[Y1][X0] + cum[Y0][X0]) / area


def calibrate_boxes(lines: list[WireLine], im: Image.Image, sent: tuple[int, int],
                    ink=None) -> tuple[list[WireLine], str]:
    """Choisit, pour la réponse d'un moteur, l'échelle de ses boîtes d'après l'encre de la page.

    La consigne demande des coordonnées 0–1000, mais un modèle peut renvoyer des pixels de l'image
    reçue (`sent`) ou des fractions 0–1. Une petite image (photo compressée par une messagerie) rend
    les pixels indiscernables du 0–1000 par les seules valeurs : on garde l'échelle dont les boîtes
    tombent le mieux sur l'écriture. Sans gain net, la consigne (0–1000) est conservée."""
    boxes = [ln.bbox for ln in lines if len(ln.bbox) == 4]
    if len(boxes) < 3:
        return lines, "0-1000"
    cum, w, h = ink or ink_map(im)
    sw, sh = sent
    cands = {"0-1000": (1.0, 1.0), f"pixels {sw}x{sh}": (1000 / sw, 1000 / sh)}
    if max(max(b) for b in boxes) <= 1.5:
        cands["0-1"] = (1000.0, 1000.0)

    def score(sx, sy):
        ds = [_ink_density(cum, w, h, (b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy)) for b in boxes]
        inside = [d for d in ds if d is not None]
        return (sum(inside) / len(ds)) if inside else 0.0

    scores = {k: score(*v) for k, v in cands.items()}
    best = max(scores, key=scores.get)
    if best == "0-1000" or scores[best] < 1.15 * scores["0-1000"]:
        return lines, "0-1000"
    sx, sy = cands[best]
    log.info("boîtes recalibrées (%s) : encre %.3f contre %.3f", best, scores[best], scores["0-1000"])
    return [ln.model_copy(update={"bbox": [round(ln.bbox[0] * sx), round(ln.bbox[1] * sy), round(ln.bbox[2] * sx),
                                           round(ln.bbox[3] * sy)]}) if len(ln.bbox) == 4 else ln
            for ln in lines], best


# -- Consensus -----------------------------------------------------------------------------


def line_uncertainties(lid: str, anchor: str, group: dict[str, WireLine], n_engines: int,
                       uid: int) -> tuple[str, list[Uncertainty], int]:
    """Compare les lectures d'une même ligne ; retourne (texte retenu, incertitudes, prochain id).

    Chaque fragment divergent devient une incertitude dont les lectures sont pondérées par le
    nombre de moteurs qui les soutiennent ; la lecture majoritaire est retenue dans le texte
    (en cas d'égalité, celle du moteur d'ancrage)."""
    a = group[anchor]
    a_norm = normalize(a.text)
    text = a.text
    uncs: list[Uncertainty] = []
    spans: dict[tuple[int, int], dict[str, set[str]]] = {}
    ta = tokens(a_norm)
    for e, g in group.items():
        if e == anchor or normalize(g.text) == a_norm:
            continue
        b_norm = normalize(g.text)
        tb = tokens(b_norm)
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
    if spans:
        text = a_norm  # les positions des fragments sont celles du texte normalisé
    replacements = []
    for (s0, s1), readings in sorted(spans.items()):
        a_seg = a_norm[s0:s1]
        if len({_layout_key(t) for t in readings}) == 1:
            continue  # mise en page seule ($, &, sauts de rangée) : aucun effet sur les mathématiques
        for e, g in group.items():
            if normalize(g.text) == a_norm:
                readings[a_seg].add(e)
        tot = sum(len(v) for v in readings.values())
        rs = sorted((Reading(text=t, support=sorted(v), score=round(len(v) / tot, 3)) for t, v in readings.items()),
                    key=lambda r: (-r.score, r.text != a_seg))
        if rs[0].text != a_seg:
            replacements.append((s0, s1, rs[0].text))
        uncs.append(Uncertainty(id=f"U{uid:02d}", line_id=lid, span=rs[0].text, readings=rs, chosen=rs[0].text,
                                reason="lectures divergentes entre moteurs"))
        uid += 1
    for s0, s1, rep in sorted(replacements, reverse=True):  # de droite à gauche : positions stables
        text = text[:s0] + rep + text[s1:]
    # Incertitudes déclarées par les moteurs eux-mêmes
    for e, g in group.items():
        for sp in g.uncertain:
            if sp.span not in text or len(sp.alternatives) < 2:
                continue
            u = next((u for u in uncs if u.span == sp.span), None)
            if u is not None:
                for alt in sp.alternatives:
                    if all(r.text != alt.text for r in u.readings):
                        u.readings.append(Reading(text=alt.text, support=[e],
                                                  score=round(alt.probability / max(1, n_engines), 3)))
                u.context_dependent |= sp.context_based
                continue
            rs = [Reading(text=x.text, support=[e], score=round(x.probability, 3)) for x in sp.alternatives]
            chosen = sp.span if any(r.text == sp.span for r in rs) else rs[0].text
            rs.sort(key=lambda r: (r.text != chosen, -r.score))
            uncs.append(Uncertainty(id=f"U{uid:02d}", line_id=lid, span=sp.span, readings=rs, chosen=chosen,
                                    reason=f"{e} : {sp.reason}", context_dependent=sp.context_based,
                                    replace_all=_is_letter(sp.span) and all(_is_letter(r.text) for r in rs)))
            uid += 1
    return text, uncs, uid


_LAYOUT = re.compile(r"\\(?:begin|end)\{[a-z*]+\}|\\text|\\sout|\\\\|[${}&\s]")


_LAYOUT_ONLY = re.compile(r"\\(?:begin|end)\{(?:cases|array|aligned|matrix)\}|\\\\|[$&\s]")


def _layout_key(text: str) -> str:
    """Fragment sans délimiteurs de mise en page ; les accolades, qui changent le sens, restent."""
    return _LAYOUT_ONLY.sub("", text)


def _skeleton(text: str) -> str:
    """Texte sans mise en page LaTeX (systèmes, cellules, $) : pour reconnaître un même contenu."""
    return _LAYOUT.sub("", normalize(text)).lower()


def _is_fragment_of(text: str, box, lines: list[TranscribedLine]) -> bool:
    """Vrai si `text` est déjà contenu dans une ligne retenue à la même hauteur.

    Un moteur peut couper en deux une ligne qu'un autre lit d'un seul tenant (deux systèmes
    côte à côte, une note dans la marge) ; sans ce test, le morceau réapparaîtrait en double."""
    t = _skeleton(text)
    if len(t) < 3:
        return False
    for ln in lines:
        # Même hauteur ou ligne voisine : les boîtes d'un système sur deux rangées sont souvent
        # celles de sa première rangée seulement.
        gap = max(box[1], ln.bbox[1]) - min(box[3], ln.bbox[3])
        if gap > max(box[3] - box[1], ln.bbox[3] - ln.bbox[1]):
            continue
        if fuzz.partial_ratio(t, _skeleton(ln.text)) >= 85:
            return True
    return False


def consensus(page: int, runs: dict[str, list[WireLine]], width: int, height: int,
              uid_start: int = 1, im: Image.Image | None = None,
              notes: dict[str, str] | None = None) -> tuple[list[TranscribedLine], list[Uncertainty]]:
    fixed = {e: normalize_boxes(ls) for e, ls in runs.items()}
    if im is not None:
        sent = im.copy()
        sent.thumbnail((LLM_MAX_SIDE, LLM_MAX_SIDE))
        ink = ink_map(im)
        for e, (ls, conf) in list(fixed.items()):
            ls2, scale = calibrate_boxes(ls, im, sent.size, ink)
            fixed[e] = (ls2, conf)
            if scale != "0-1000" and notes is not None:
                notes[e] = f"boîtes recalibrées ({scale})"
    runs = {e: v[0] for e, v in fixed.items()}
    # Moteur d'ancrage (positions des lignes) : celui dont les boîtes étaient les plus conformes ;
    # à égalité, l'ordre donné par l'utilisateur.
    order = list(runs)
    engines = sorted([e for e, ls in runs.items() if ls], key=lambda e: (-round(fixed[e][1], 2), order.index(e)))
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
                sc = 0.6 * _y_iou(a.bbox, b.bbox) + 0.4 * fuzz.ratio(normalize(a.text), normalize(b.text)) / 100
                if sc > best_s:
                    best, best_s = j, sc
            if best is not None:
                used[e].add(best)
                group[e] = runs[e][best]
        n = len(engines)
        agree = sum(1 for g in group.values() if normalize(g.text) == normalize(a.text))
        conf = min(g.confidence for g in group.values()) * (agree / n if n > 1 else 1.0)
        if len(group) < n:
            conf *= 0.7
        text, line_uncs, uid = line_uncertainties(lid, anchor, group, n, uid)
        lines.append(TranscribedLine(id=lid, page=page, bbox=px(a.bbox), text=text,
                                     status=LineStatus(a.status), confidence=round(conf, 3),
                                     engine_readings={e: g.text for e, g in group.items()}))
        uncs += line_uncs
    # Lignes vues par d'autres moteurs mais pas par l'ancre : contenu possiblement manqué.
    k = len(lines)
    for e in others:
        for j, b in enumerate(runs[e]):
            if j in used[e] or b.status != "normal":
                continue
            if _is_fragment_of(b.text, px(b.bbox), lines):
                continue  # morceau d'une ligne que l'ancre a lue d'un seul tenant : rien de manqué
            k += 1
            lines.append(TranscribedLine(id=f"p{page}.L{k:02d}", page=page, bbox=px(b.bbox), text=b.text,
                                         status=LineStatus.normal, confidence=round(b.confidence * 0.4, 3),
                                         engine_readings={e: b.text}))
    lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    return lines, uncs


# -- Arbitrage ------------------------------------------------------------------------------


def span_boxes(ln: TranscribedLine, span: str, width: float = 0.35) -> list[tuple[int, int, int, int]]:
    """Boîtes approximatives des occurrences (3 au plus) d'un fragment court dans sa ligne.

    Position dans le texte → position dans la boîte, avec une marge large (35 % de la ligne) : c'est un gros
    plan, pas une localisation exacte."""
    strip = re.compile(r"\\[A-Za-z]+|[${}]")
    if not span or len(span.strip()) > 3 or len(ln.bbox) != 4 or not 1 <= ln.text.count(span) <= 3:
        return []
    visible, frag = strip.sub("", ln.text), strip.sub("", span)
    if not visible or not frag:
        return []
    x0, y0, x1, y1 = ln.bbox
    out, start = [], 0
    while (i := ln.text.find(span, start)) >= 0:
        c = x0 + (x1 - x0) * (len(strip.sub("", ln.text[:i])) + len(frag) / 2) / len(visible)
        half = (x1 - x0) * width / 2
        out.append((int(max(x0, c - half)), y0, int(min(x1, c + half)), y1))
        start = i + len(span)
    return out


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
        boxes = span_boxes(ln, u.span)  # chiffre, signe, exposant : gros plan sur le fragment lui-même
        for k, zoom in enumerate(boxes, start=1):
            label = f"{u.id} — gros plan sur « {u.span} »" + (f" (occurrence {k}/{len(boxes)})" if len(boxes) > 1 else "")
            images.append(to_part(crop_line(page_img, zoom, margin=0.25), label, max_side=1200))
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
        # L'arbitre a tranché sur la forme en comparant avec les autres tracés de l'élève : c'est son
        # jugement (et non celui d'un lecteur à l'aveugle) qui dit si le contexte a été utilisé.
        u.context_dependent = d.context_based
        if u.readings[0].text != u.chosen:
            ln = by_line[u.line_id]
            new = u.readings[0].text
            if _is_letter(u.span) and _is_letter(new):
                # Lettre de variable (k/h…) : on renomme TOUTES les occurrences de la ligne, sinon
                # la formule devient incohérente (indice h, terme en k).
                ln.text = _rename_letter(ln.text, u.span, new)
                u.replace_all = True
            elif ln.text.count(u.span) == 1:
                ln.text = ln.text.replace(u.span, new, 1)
            else:
                # Fragment non unique : on ne sait pas quelle occurrence modifier ; on garde la lecture
                # d'origine et le doute reste signalé.
                u.reason += " ; arbitrage non appliqué (fragment présent plusieurs fois dans la ligne)"
                continue
            u.chosen = u.span = new


# -- Cascade : accord d'abord, zoom sur les lignes disputées ---------------------------------


def disputed_lines(lines: list[TranscribedLine], uncs: list[Uncertainty], n_engines: int,
                   min_conf: float) -> set[str]:
    """Lignes à relire par un moteur fort : désaccord, doute déclaré, confiance faible, ligne manquée."""
    out = {u.line_id for u in uncs}
    for ln in lines:
        if ln.status != LineStatus.normal:
            continue
        if ln.confidence < min_conf or len(ln.engine_readings) < n_engines:
            out.add(ln.id)
    return out


def audit_sample(lines: list[TranscribedLine], exclude: set[str], rate: float, seed: str) -> set[str]:
    """Échantillon reproductible de lignes « d'accord » relues quand même (contrôle qualité continu)."""
    if rate <= 0:
        return set()
    pool = [ln.id for ln in lines if ln.status == LineStatus.normal and ln.id not in exclude]
    k = min(len(pool), max(1, round(rate * len(pool)))) if pool else 0
    ranked = sorted(pool, key=lambda lid: hashlib.sha256((seed + lid).encode()).hexdigest())
    return set(ranked[:k])


def read_crops(engine: Engine, page_img: Image.Image, lines: list[TranscribedLine]) -> dict[str, WireLine]:
    images = [to_part(crop_line(page_img, ln.bbox), ln.id, max_side=1600) for ln in lines]
    out = engine.structured(prompts.OCR_SYSTEM, prompts.OCR_CROPS_TASK + "\nIdentifiants : "
                            + ", ".join(ln.id for ln in lines), images, WireCropReadings)
    return {r.id: WireLine(bbox=[0, 0, 0, 0], text=r.text, confidence=r.confidence, uncertain=r.uncertain)
            for r in out.items if r.id in {ln.id for ln in lines}}


def cascade_page(page: int, im: Image.Image, runs: dict[str, list[WireLine]], strong: list[Engine],
                 cfg, seed: str, uid_start: int) -> tuple[list[TranscribedLine], list[Uncertainty], list[EngineRun]]:
    lines, uncs = consensus(page, runs, im.width, im.height, uid_start, im=im)
    n = len([e for e, ls in runs.items() if ls])
    disputed = disputed_lines(lines, uncs, n, getattr(cfg, "cascade_min_conf", 0.75))
    audited = audit_sample(lines, disputed, getattr(cfg, "audit_rate", 0.0), seed)
    to_read = [ln for ln in lines if ln.id in disputed | audited]
    meta: list[EngineRun] = []
    if not to_read or not strong:
        return lines, uncs, meta
    strong_readings: dict[str, dict[str, WireLine]] = {}
    for eng in strong:
        t0 = time.monotonic()
        try:
            strong_readings[eng.name] = read_crops(eng, im, to_read)
            meta.append(EngineRun(engine=eng.name, model=eng.model, mode="cascade_lignes", ok=True,
                                  seconds=round(time.monotonic() - t0, 1),
                                  note=f"{len(to_read)} ligne(s) relue(s) sur {len(lines)}"))
        except Exception as ex:  # noqa: BLE001
            meta.append(EngineRun(engine=eng.name, model=eng.model, mode="cascade_lignes", ok=False, error=str(ex)))
    if not strong_readings:
        return lines, uncs, meta
    uid = max([int(u.id[1:]) for u in uncs] + [uid_start - 1]) + 1
    audit_disagree = 0
    by_id = {ln.id: ln for ln in lines}
    for ln in to_read:
        group: dict[str, WireLine] = {}
        anchor = None
        for e, rd in strong_readings.items():
            if ln.id in rd:
                group[e] = rd[ln.id]
                anchor = anchor or e
        if anchor is None:
            continue
        for e, txt in ln.engine_readings.items():
            group.setdefault(e, WireLine(bbox=[0, 0, 0, 0], text=txt, confidence=ln.confidence))
        if ln.id in audited and any(normalize(g.text) != normalize(ln.text) for g in group.values()):
            audit_disagree += 1
        text, new_uncs, uid = line_uncertainties(ln.id, anchor, group, len(group), uid)
        uncs = [u for u in uncs if u.line_id != ln.id] + new_uncs
        agree = sum(1 for g in group.values() if normalize(g.text) == normalize(text))
        by_id[ln.id].text = text
        by_id[ln.id].confidence = round(min(g.confidence for g in group.values()) * agree / len(group), 3)
        by_id[ln.id].engine_readings = {e: g.text for e, g in group.items()}
    if audited:
        meta.append(EngineRun(engine="audit", model="", mode="audit", ok=True,
                              note=f"{audit_disagree} désaccord(s) sur {len(audited)} ligne(s) d'accord auditée(s)"))
    uncs.sort(key=lambda u: int(u.id[1:]))
    return lines, uncs, meta


def _is_letter(s: str) -> bool:
    return re.fullmatch(r"[A-Za-z]", s.strip()) is not None


def _rename_letter(text: str, old: str, new: str) -> str:
    """Renomme une lettre de variable dans les parties mathématiques ($…$), hors commandes LaTeX."""
    def fix(m):
        return re.sub(rf"(?<![\\A-Za-z]){re.escape(old)}(?![A-Za-z])", new, m.group(0))
    return re.sub(r"\$[^$]*\$", fix, text)


# -- Point d'entrée ----------------------------------------------------------------------------


# Résolutions de l'analyse de sensibilité pour lesquelles l'arbitre ne peut rien changer au verdict.
NO_EFFECT = ("mal formée", "même sens", "même valeur", "même énoncé", "même définition", "hors de l'énoncé",
             "n'affecte aucune étape")


def needs_judge(u: Uncertainty) -> bool:
    """Un doute mérite l'arbitre si au moins une lecture alternative pourrait changer une étape."""
    if u.blocking:
        return True
    parts = [p.strip() for p in (u.resolution or "").split(" ; ") if p.strip()]
    return any(not any(k in p for k in NO_EFFECT) for p in parts)


def adjudicate_needed(tr: Transcription, ref: ReferenceStatement, cfg) -> list[tuple[Uncertainty, str]]:
    """Arbitre les seuls doutes utiles ; retourne les lectures changées [(doute, ancienne lecture)]."""
    todo = [u for u in tr.uncertainties if needs_judge(u)]
    if not todo:
        tr.engines.append(EngineRun(engine=cfg.reasoning_engine, model="", mode="arbitrage", ok=True,
                                    note="aucun doute ne peut changer le verdict : arbitre non appelé"))
        return []
    before = {u.id: u.chosen for u in todo}
    cache = Path(getattr(cfg, "cache_dir", "runs/.cache"))
    try:
        eng = make_engine(cfg.reasoning_engine, cache)
        for pg in tr.pages:
            _, im = load_page(Path(pg.path), pg.page)
            lines = [ln for ln in tr.lines if ln.page == pg.page]
            ids = {ln.id for ln in lines}
            adjudicate(eng, ref, im, lines, [u for u in todo if u.line_id in ids])
        tr.engines.append(EngineRun(engine=cfg.reasoning_engine, model=eng.model, mode="arbitrage", ok=True,
                                    note=f"{len(todo)} doute(s) arbitré(s) sur {len(tr.uncertainties)}"))
    except Exception as ex:  # noqa: BLE001
        tr.engines.append(EngineRun(engine=cfg.reasoning_engine, model="", mode="arbitrage", ok=False, error=str(ex)))
        return []
    return [(u, before[u.id]) for u in todo if u.chosen != before[u.id]]


def meta_page_seed(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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

        def read(spec):
            """Lecture aveugle d'un moteur ; retourne (nom, lignes, méta)."""
            t0 = time.monotonic()
            try:
                if spec.startswith("mathpix"):
                    eng = make_engine(spec)
                    scale = min(1.0, 4000 / max(im.size))
                    rw, rh = im.width * scale, im.height * scale
                    raw = eng.ocr_lines(to_part(im, f"page {pno}", max_side=4000))
                    from ..llm.pricing import record
                    record(engine=eng.name, model="mathpix", images=1)
                    lines = [WireLine(bbox=[int(b["bbox"][0] * 1000 / rw), int(b["bbox"][1] * 1000 / rh),
                                            int(b["bbox"][2] * 1000 / rw), int(b["bbox"][3] * 1000 / rh)],
                                      text=b["text"], confidence=b["confidence"]) for b in raw]
                else:
                    eng = make_engine(spec, cache)
                    lines = eng.structured(prompts.OCR_SYSTEM, prompts.OCR_TASK, [part], WirePage,
                                           effort=getattr(cfg, "ocr_effort", "high")).lines
                return eng.name, lines, EngineRun(engine=eng.name, model=eng.model, mode="aveugle", ok=True,
                                                  seconds=round(time.monotonic() - t0, 1))
            except Exception as ex:  # noqa: BLE001 - un moteur défaillant ne doit pas arrêter les autres
                log.warning("moteur %s indisponible : %s", spec, ex)
                return spec, None, EngineRun(engine=spec, model="", mode="aveugle", ok=False, error=str(ex))

        # Les lecteurs sont indépendants (lecture à l'aveugle) : ils lisent en même temps.
        import contextvars
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, len(cfg.ocr_engines))) as pool:
            # Contexte copié dans le fil principal (compteur de coûts) puis exécuté dans chaque fil.
            jobs = [(contextvars.copy_context(), sp) for sp in cfg.ocr_engines]
            results = list(pool.map(lambda job: job[0].run(read, job[1]), jobs))
        for name, lines, meta_run in results:  # ordre donné par l'utilisateur (moteur d'ancrage à égalité)
            if lines is not None:
                runs[name] = lines
            runs_meta.append(meta_run)
        if not any(runs.values()):
            raise EngineError("aucun moteur OCR n'a répondu")
        if getattr(cfg, "ocr_mode", "ensemble") == "cascade":
            strong = []
            for spec in getattr(cfg, "ocr_strong", []) or []:
                try:
                    strong.append(make_engine(spec, cache))
                except Exception as ex:  # noqa: BLE001
                    runs_meta.append(EngineRun(engine=spec, model="", mode="cascade_lignes", ok=False, error=str(ex)))
            lines, uncs, meta = cascade_page(pno, im, runs, strong, cfg, seed=meta_page_seed(path),
                                             uid_start=len(all_uncs) + 1)
            runs_meta += meta
        else:
            notes: dict[str, str] = {}
            lines, uncs = consensus(pno, runs, im.width, im.height, uid_start=len(all_uncs) + 1, im=im, notes=notes)
            for r in runs_meta:
                if r.mode == "aveugle" and r.engine in notes:
                    r.note = notes[r.engine]
        if cfg.reasoning_engine and not getattr(cfg, "lazy_judge", False):
            try:
                adjudicate(make_engine(cfg.reasoning_engine, cache), ref, im, lines, uncs)
                runs_meta.append(EngineRun(engine=cfg.reasoning_engine, model="", mode="arbitrage", ok=True))
            except Exception as ex:  # noqa: BLE001
                runs_meta.append(EngineRun(engine=cfg.reasoning_engine, model="", mode="arbitrage", ok=False, error=str(ex)))
        all_lines += lines
        all_uncs += uncs
    ok = [r.engine for r in runs_meta if r.ok and r.mode == "aveugle"]
    cascade = [r.engine for r in runs_meta if r.ok and r.mode == "cascade_lignes"]
    return Transcription(pages=pages, lines=all_lines, uncertainties=all_uncs, engines=runs_meta,
                         provenance=f"Consensus de {len(ok)} moteur(s) en lecture aveugle ({', '.join(ok)})"
                                    + (f", lignes disputées relues en zoom par {', '.join(cascade)}" if cascade else "")
                                    + (f", arbitrage par {cfg.reasoning_engine}" if cfg.reasoning_engine else "") + ".")
