import json

from conftest import EX
from leanonsteroids.schemas import Transcription, Uncertainty, Reading
from leanonsteroids.stages.evaluate import _math, score
from leanonsteroids.stages.transcribe import tokens

REF = Transcription(**json.loads((EX / "fixtures" / "transcription.json").read_text()))


def test_perfect_reading():
    sc = score(REF, REF.model_copy(deep=True))
    assert sc.math_errors == 0 and sc.silent_errors == 0 and not sc.missed_lines


def test_silent_flagged_and_missed_are_distinguished():
    hyp = REF.model_copy(deep=True)
    hyp.uncertainties = []
    by = {ln.id: ln for ln in hyp.lines}
    # Erreur silencieuse : l'exposant lu « 3 », sans aucun doute déclaré.
    by["p1.L12"].text = r"$= (n+1)^3$"
    # Erreur signalée : « - » au lieu de « + », mais un moteur a déclaré le doute.
    by["p1.L11"].text = r"$= n^2 + (2n-1)$ (d'après H.R.)"
    hyp.uncertainties.append(Uncertainty(id="U01", line_id="p1.L11", span="(2n-1)", chosen="(2n-1)",
                                         readings=[Reading(text="(2n-1)", score=0.6), Reading(text="(2n+1)", score=0.4)],
                                         reason="test"))
    # Ligne de calcul manquée.
    hyp.lines = [ln for ln in hyp.lines if ln.id != "p1.L16"]
    sc = score(REF, hyp)
    assert sc.missed_lines == ["p1.L16"]
    silent = [e for e in sc.errors if not e.flagged]
    flagged = [e for e in sc.errors if e.flagged]
    assert [(e.expected, e.got) for e in silent] == [("2", "3")]
    assert [(e.expected, e.got) for e in flagged] == [("+", "-")]
    missed_tokens = len(tokens(_math(next(ln for ln in REF.lines if ln.id == 'p1.L16').text)))
    assert sc.silent_errors == 1 + missed_tokens  # l'exposant + tous les jetons de la ligne manquée


def test_consistent_index_renaming_is_not_an_error():
    hyp = REF.model_copy(deep=True)
    for ln in hyp.lines:
        if ln.id in ("p1.L02", "p1.L08"):
            ln.text = ln.text.replace("{k=0}", "{h=0}").replace("2k", "2h")
    sc = score(REF, hyp)
    assert sc.math_errors == 0 and sc.renamed_indices == 2
    # …mais un renommage partiel (indice h, terme en k) reste une erreur.
    hyp2 = REF.model_copy(deep=True)
    next(ln for ln in hyp2.lines if ln.id == "p1.L02").text = r"$P(n) : \sum_{h=0}^{n-1} (2k+1) = n^2$"
    assert score(REF, hyp2).math_errors == 1
