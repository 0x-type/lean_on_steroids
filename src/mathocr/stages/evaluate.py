"""Évaluation de la lecture des copies : mesurer avant d'économiser.

On compare une transcription produite par une configuration de moteurs à une
transcription de référence vérifiée à la main. Mesures :

* lignes trouvées / manquées (une ligne de calcul manquée est grave) ;
* erreurs sur les jetons mathématiques (le français compte peu pour la correction) ;
* **erreurs silencieuses** : jetons mal lus qu'aucune incertitude ne signale. Une
  erreur signalée est rattrapée en aval (analyse de sensibilité, puis humain) ;
  une erreur silencieuse peut fausser le verdict. C'est la mesure qui décide ;
* coût de la lecture.

Règle d'adoption d'une configuration moins chère : pas plus d'erreurs
silencieuses ni de lignes manquées que la configuration de référence sur le
jeu d'évaluation (et, si l'option verdict est utilisée, aucun verdict changé).
"""

from __future__ import annotations

import difflib
import re

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from ..schemas import LineStatus, Transcription
from .transcribe import _y_iou, normalize, tokens


class LineError(BaseModel):
    ref_line: str
    hyp_line: str
    expected: str
    got: str
    flagged: bool


class OcrScore(BaseModel):
    case: str = ""
    config: str = ""
    ref_lines: int = 0
    matched_lines: int = 0
    missed_lines: list[str] = Field(default_factory=list)
    math_tokens: int = 0
    math_errors: int = 0
    silent_errors: int = 0
    flagged_errors: int = 0
    uncertainties: int = 0
    cost_usd: float | None = None
    errors: list[LineError] = Field(default_factory=list)

    @property
    def token_error_rate(self) -> float:
        return self.math_errors / self.math_tokens if self.math_tokens else 0.0


def _math(text: str) -> str:
    segs = re.findall(r"\$([^$]*)\$", text)
    return normalize(" ".join(segs)) if segs else ""


def _align(ref: Transcription, hyp: Transcription) -> dict[str, str]:
    pairs: dict[str, str] = {}
    used: set[str] = set()
    for r in ref.lines:
        if r.status != LineStatus.normal:
            continue
        best, best_s = None, 0.45
        for h in hyp.lines:
            if h.id in used or h.page != r.page:
                continue
            sc = 0.5 * _y_iou(r.bbox, h.bbox) + 0.5 * fuzz.ratio(normalize(r.text), normalize(h.text)) / 100
            if sc > best_s:
                best, best_s = h.id, sc
        if best is not None:
            pairs[r.id] = best
            used.add(best)
    return pairs


def _flagged(hyp_norm: str, start: int, end: int, spans: list[str]) -> bool:
    """Une erreur est signalée si une incertitude de la ligne recouvre sa position."""
    for sp in spans:
        sp_n = normalize(sp)
        if not sp_n:
            continue
        pos = hyp_norm.find(sp_n)
        while pos != -1:
            if pos <= max(start, end - 1) and pos + len(sp_n) >= start:
                return True
            pos = hyp_norm.find(sp_n, pos + 1)
    return False


def score(ref: Transcription, hyp: Transcription, *, case: str = "", config: str = "") -> OcrScore:
    sc = OcrScore(case=case, config=config, uncertainties=len(hyp.uncertainties))
    pairs = _align(ref, hyp)
    hyp_by = {h.id: h for h in hyp.lines}
    for r in ref.lines:
        if r.status != LineStatus.normal:
            continue
        sc.ref_lines += 1
        rm = _math(r.text)
        rt = tokens(rm)
        sc.math_tokens += len(rt)
        if r.id not in pairs:
            if rt:
                sc.missed_lines.append(r.id)
                sc.math_errors += len(rt)
                sc.silent_errors += len(rt)
            continue
        sc.matched_lines += 1
        h = hyp_by[pairs[r.id]]
        hm = _math(h.text)
        ht = tokens(hm)
        spans = [u.span for u in hyp.uncertainties if u.line_id == h.id]
        low_conf = h.confidence < 0.5
        sm = difflib.SequenceMatcher(a=[t[0] for t in rt], b=[t[0] for t in ht], autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            n_err = max(i2 - i1, j2 - j1)
            exp = rm[rt[i1][1]:rt[i2 - 1][2]] if i2 > i1 else ""
            got = hm[ht[j1][1]:ht[j2 - 1][2]] if j2 > j1 else ""
            hs = ht[j1][1] if j2 > j1 else (ht[j1 - 1][2] if j1 > 0 else 0)
            he = ht[j2 - 1][2] if j2 > j1 else hs
            flagged = low_conf or _flagged(hm, hs, he, spans)
            sc.math_errors += n_err
            if flagged:
                sc.flagged_errors += n_err
            else:
                sc.silent_errors += n_err
            sc.errors.append(LineError(ref_line=r.id, hyp_line=h.id, expected=exp, got=got, flagged=flagged))
    return sc


def table(scores: list[OcrScore]) -> str:
    head = "| cas | configuration | lignes manquées | erreurs maths | dont silencieuses | taux | coût |\n|---|---|---|---|---|---|---|"
    rows = [f"| {s.case} | {s.config} | {len(s.missed_lines)} | {s.math_errors}/{s.math_tokens} | "
            f"**{s.silent_errors}** | {s.token_error_rate:.1%} | "
            f"{'—' if s.cost_usd is None else f'{s.cost_usd:.4f} $'} |" for s in scores]
    return "\n".join([head, *rows])
