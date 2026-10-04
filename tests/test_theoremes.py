"""Théorèmes cités : repérage dans la copie et jugement des théorèmes utilisés par la preuve Lean."""

import json

import pytest

from leanonsteroids.theoremes import Catalogue, Theoreme, juger_usages, reperer


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
    from leanonsteroids import theoremes
    from leanonsteroids.schemas import Citation, ProofStep, ProofStructure, SourceRef, TranscribedLine, Transcription

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


def test_pluriels_ignores_sigles_respectes(cat):
    from leanonsteroids.theoremes import Theoreme
    cat.theoremes["fact"] = Theoreme(cle="fact", nom="décomposition", alias=["produit de facteurs premiers"],
                                     lemmes=["x"], verifie=True)
    assert reperer("se décompose en un produit de facteur premiers", cat) == ["fact"]
    assert reperer("on applique TVA", cat) == []


@pytest.mark.parametrize("a,b,change", [
    ("singleton", "singlton", False), ("est constante", "n'est pas constante", True),
    ("croissante", "décroissante", True), ("continue", "continu", False), ("et", "ou", True),
    ("positive", "positif", False), ("pair", "impair", True), ("majorée", "minorée", True)])
def test_seuls_les_mots_logiques_bloquent(a, b, change):
    from leanonsteroids.stages.fidelity import _logic_change, normalize_notation
    assert _logic_change(a, b) is change
    assert normalize_notation(r"$A \Rightarrow B$") == normalize_notation(r"$A \implies B$")


def test_regle_de_simplification_est_un_fait_de_base(cat):
    used = [("MultilinearMap.sum_apply", "Mathlib.LinearAlgebra.Multilinear.Basic", True)]
    assert juger_usages([], used, cat) == (True, "preuve élémentaire")
    used_named = [("intermediate_value_univ", "Mathlib.Topology.Order.IntermediateValue", True)]
    assert juger_usages([], used_named, cat)[0] is False  # un théorème du catalogue reste un grand théorème


@pytest.mark.parametrize("tolerance,blocks", [("tolerant", False), ("strict", True)])
def test_justification_elementaire_absente_selon_le_mode(tolerance, blocks):
    from leanonsteroids.schemas import (Formalization, LeanReport, ProofStep, ProofStructure, ReferenceStatement, SourceRef,
                                 StepCheck, StepFormal, Transcription)
    from leanonsteroids.stages.verdict import decide
    ref = ReferenceStatement(exercise_id="t", statement_latex="", lean_statement="True", validated_by="test")
    st = ProofStructure(scopes=[], pattern={"kind": "aucun"}, steps=[
        ProofStep(id="s1", kind="affirmation", statement="x", source=[SourceRef(line_id="p1.L01", excerpt="x")])])
    lean = LeanReport(file="", steps=[StepCheck(step_id="s1", decl="s1", status="verifie_elementaire",
                                                closed_by="preuve élémentaire proposée au niveau 2 (…)")])
    fm = Formalization(scopes=[], steps=[StepFormal(step_id="s1", role="prop", claim="True")])
    vr = decide(ref, Transcription(pages=[], lines=[]), st, lean, [], fm, tolerance=tolerance)
    assert any("s1" in b and "saut logique" in b for b in vr.blocking_issues) is blocks
    assert any("mode tolérant" in r for r in vr.remarks) is (not blocks)


def test_ponctuation_seule_ne_bloque_pas():
    from leanonsteroids.stages.fidelity import _sans_ponctuation
    assert _sans_ponctuation(",") == _sans_ponctuation(";")
    assert _sans_ponctuation("[a, b]") == _sans_ponctuation("[a ; b]")
    assert _sans_ponctuation("x = 1") != _sans_ponctuation("x = 2")


def test_lemme_du_kit_qui_est_un_grand_theoreme_exige_une_citation(cat):
    """Cas réel (B06) : le TVI du kit fermait « f est constante » sans que la copie cite le TVI. Une copie qui
    oublie complètement le TVI aurait été « vérifiée » : un pas difficile absent n'est jamais toléré."""
    from leanonsteroids.theoremes import cles_kit, juger_assemblage
    kt = {"Kit.tvi_annulation": "tvi"}
    used = [("Kit.tvi_annulation", "Kit"), ("Kit.valeurs_possibles", "Kit"), ("le_of_not_gt", "Mathlib.Order.Defs")]
    ok, why = juger_usages([], used, cat, kit_theoremes=kt)
    assert not ok and "valeurs intermédiaires" in why
    assert juger_usages([], [("Kit.valeurs_possibles", "Kit")], cat, kit_theoremes=kt)[0]  # lemme élémentaire
    # Le TVI est cité dans la copie (pas forcément à cette étape) : l'étape s'appuie sur cette citation.
    assert cles_kit(used, kt, ["tvi"]) == ["tvi"] and cles_kit(used, kt, []) == []
    ok, why = juger_usages(["tvi"], used, cat, kit_theoremes=kt)
    assert ok and "Kit.tvi_annulation" in why
    glue = [("Copie.s2", "Copie"), ("Kit.tvi_annulation", "Kit")]
    assert not juger_assemblage("exact Kit.tvi_annulation", glue, kt, [])[0]
    assert juger_assemblage("exact Kit.tvi_annulation", glue, kt, ["tvi"])[0]


def test_lemme_du_kit_qui_est_un_grand_theoreme_exige_une_citation(cat):
    """Cas réel (B06) : le TVI du kit fermait « f est constante » sans que la copie cite le TVI. Une copie qui
    oublie complètement le TVI aurait été « vérifiée » : un pas difficile absent n'est jamais toléré."""
    from leanonsteroids.theoremes import cles_kit, juger_assemblage
    kt = {"Kit.tvi_annulation": "tvi"}
    used = [("Kit.tvi_annulation", "Kit"), ("Kit.valeurs_possibles", "Kit"), ("le_of_not_gt", "Mathlib.Order.Defs")]
    ok, why = juger_usages([], used, cat, kit_theoremes=kt)
    assert not ok and "valeurs intermédiaires" in why
    assert juger_usages([], [("Kit.valeurs_possibles", "Kit")], cat, kit_theoremes=kt)[0]  # lemme élémentaire
    # Le TVI est cité dans la copie (pas forcément à cette étape) : l'étape s'appuie sur cette citation.
    assert cles_kit(used, kt, ["tvi"]) == ["tvi"] and cles_kit(used, kt, []) == []
    ok, why = juger_usages(["tvi"], used, cat, kit_theoremes=kt)
    assert ok and "Kit.tvi_annulation" in why
    glue = [("Copie.s2", "Copie"), ("Kit.tvi_annulation", "Kit")]
    assert not juger_assemblage("exact Kit.tvi_annulation", glue, kt, [])[0]
    assert juger_assemblage("exact Kit.tvi_annulation", glue, kt, ["tvi"])[0]


def test_sigle_a_une_lettre_pres(cat):
    """« TVA » écrit pour « TVI » (copie B06) : accepté comme citation, que Lean doit encore confirmer."""
    assert reperer("on applique TVA sur [x_1, x_2]", cat, approx=True) == ["tvi"]
    assert reperer("on applique T.V.A sur [a, b]", cat, approx=True) == ["tvi"]
    assert reperer("on applique TVA", cat) == []  # mode exact inchangé
    assert reperer("d'après AVI", cat, approx=True) == []  # première lettre différente
    assert reperer("la TVQX", cat, approx=True) == []  # longueur différente
    assert reperer("Tva", cat, approx=True) == []  # pas un sigle
