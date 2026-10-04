"""Théorèmes cités : repérage dans la copie et jugement des théorèmes utilisés par la preuve Lean."""

import json

import pytest

from mathocr.theoremes import Catalogue, Theoreme, juger_usages, reperer


@pytest.fixture
def cat():
    tvi = Theoreme(cle="tvi", nom="théorème des valeurs intermédiaires", alias=["TVI", "valeurs intermédiaires"],
                   lemmes=["intermediate_value_univ"], compagnons=["ordered_connected_space"], verifie=True)
    rolle = Theoreme(cle="rolle", nom="théorème de Rolle", alias=["Rolle"], lemmes=["exists_deriv_eq_zero"],
                     verifie=True)
    return Catalogue({"tvi": tvi, "rolle": rolle}, ["Mathlib.Topology", "Mathlib.Analysis"], {"Real.sqrt_nonneg"})


def test_reperage_aux_accents_et_majuscules_pres(cat):
    assert reperer("on applique le théorème des Valeurs Intermediaires", cat) == ["tvi"]
    assert reperer(r"$\text{TVI}$ , il existe c", cat) == ["tvi"]
    assert reperer("on applique TVA sur [x_1, x_2]", cat) == []  # ce que l'élève a écrit, pas ce qu'il voulait dire
    assert reperer("controle continu", cat) == []


def test_preuve_qui_utilise_le_theoreme_cite_et_lui_seul(cat):
    used = [("intermediate_value_univ", "Mathlib.Topology.Order.IntermediateValue"),
            ("ordered_connected_space", "Mathlib.Topology.Order.IntermediateValue"),
            ("lt_or_gt_of_ne", "Mathlib.Order.Defs.LinearOrder")]
    ok, why = juger_usages(["tvi"], used, cat)
    assert ok and "intermediate_value_univ" in why


def test_preuve_qui_utilise_un_theoreme_non_cite(cat):
    used = [("intermediate_value_univ", "Mathlib.Topology.Order.IntermediateValue"),
            ("exists_deriv_eq_zero", "Mathlib.Analysis.Calculus.LocalExtr.Rolle")]
    ok, why = juger_usages(["tvi"], used, cat)
    assert not ok and "exists_deriv_eq_zero" in why


def test_citation_sans_usage_et_preuve_elementaire(cat):
    elem = [("lt_or_gt_of_ne", "Mathlib.Order.Defs.LinearOrder"), ("Real.sqrt_nonneg", "Mathlib.Analysis.SpecialFunctions.Sqrt")]
    assert juger_usages(["tvi"], elem, cat)[0] is False  # cité mais pas utilisé : rien à accepter au titre du TVI
    assert juger_usages([], elem, cat) == (True, "preuve élémentaire")
    assert juger_usages([], [("exists_deriv_eq_zero", "Mathlib.Analysis.Calculus.LocalExtr.Rolle")], cat)[0] is False


def test_citation_ancree_dans_la_copie(cat, monkeypatch):
    from mathocr import theoremes
    from mathocr.schemas import Citation, ProofStep, ProofStructure, SourceRef, TranscribedLine, Transcription

    monkeypatch.setattr(theoremes, "charger", lambda path=None: cat)
    lines = [TranscribedLine(id="p1.L01", page=1, bbox=[0, 0, 1, 1], text="on applique le TVI :", status="normal",
                             confidence=0.9, engine_readings={}),
             TranscribedLine(id="p1.L02", page=1, bbox=[0, 2, 1, 3], text=r"il existe $c$ tel que $f(c)=0$",
                             status="normal", confidence=0.9, engine_readings={})]
    tr = Transcription(pages=[], lines=lines)
    st = ProofStructure(scopes=[], pattern={"kind": "aucun"}, steps=[
        ProofStep(id="s1", kind="affirmation", statement="f(c)=0", source=[SourceRef(line_id="p1.L02", excerpt="f(c)=0")]),
        # citation proposée par l'agent mais absente de la copie : refusée
        ProofStep(id="s2", kind="affirmation", statement="x", source=[SourceRef(line_id="p1.L01", excerpt="on applique")],
                  citations=[Citation(theoreme="rolle", extrait="Rolle")])])
    out = theoremes.citations_ancrees(st, tr, cat)
    assert out["s1"] == ["tvi"]  # citée sur la ligne juste avant
    assert "rolle" not in out.get("s2", [])
