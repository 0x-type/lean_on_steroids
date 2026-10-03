import pytest

from mathocr.stages.latex2lean import TranslationError, Translator


def T(types=None, preds=("P",)):
    return Translator(types or {"n": "ℕ"}, set(preds))


@pytest.mark.parametrize("latex,lean", [
    (r"\sum_{k=0}^{n-1} (2k+1) = n^2", "∑ k ∈ Finset.range n, (2 * k + 1) = n ^ 2"),
    (r"\sum_{k=0}^{n} (2k+1) = (n+1)^2", "∑ k ∈ Finset.range (n + 1), (2 * k + 1) = (n + 1) ^ 2"),
    (r"\sum_{k=1}^{n} k = \frac{n(n+1)}{2}",
     "∑ k ∈ Finset.Icc 1 n, (k : ℚ) = (((n : ℚ) * ((n : ℚ) + 1)) / (2))"),
    (r"n^2 - 1 = (n-1)(n+1)", "(n : ℤ) ^ 2 - 1 = ((n : ℤ) - 1) * ((n : ℤ) + 1)"),
    (r"P(n+1) \text{ est vraie}", "P (n + 1)"),
    (r"\sum_{k=0}^{-1} (2k+1) \text{ est nulle}", "(∑ k ∈ Finset.range 0, (2 * k + 1) : ℕ) = 0"),
    (r"P(0) \text{ et } \forall n,\ P(n) \Rightarrow P(n+1)", "P 0 ∧ (∀ n : ℕ, P n → P (n + 1))"),
    (r"P(n) \Rightarrow P(n+1) \forall n \in \mathbb{N}", "∀ n : ℕ, P n → P (n + 1)"),
    (r"n! \geq 2^{n-1}", None),  # soustraction dans un exposant : refusé
    (r"2n \leq n^2 + 1", "2 * n ≤ n ^ 2 + 1"),
])
def test_translation(latex, lean):
    if lean is None:
        with pytest.raises(TranslationError):
            T().claim(latex)
    else:
        assert T().claim(latex).lean == lean


def test_sides_keep_student_latex():
    c = T().claim(r"n^2 + (2n+1) = (n+1)^2")
    assert c.sides.lhs_latex == "n^2 + (2n+1)" and c.sides.rhs_latex == "(n+1)^2"
    assert c.sides.lhs_lean == "n ^ 2 + (2 * n + 1)"  # parenthèses conservées, pas de simplification


@pytest.mark.parametrize("bad", [
    r"\text{c'est évident}",
    r"a = b = c",
    r"\int_0^1 x dx = 1/2",
])
def test_refusals_go_to_agent(bad):
    with pytest.raises(TranslationError):
        T({"a": "ℕ", "b": "ℕ", "c": "ℕ", "x": "ℝ"}).claim(bad)


def test_real_variable_domain():
    c = T({"x": "ℝ"}).claim(r"x^2 \geq 0")
    assert c.lean == "x ^ 2 ≥ 0" and c.sides.value_type == "ℝ"


def test_incoherent_reading_is_refused_not_quantified():
    """Cas réel : l'arbitre avait renommé l'indice d'une somme sans renommer le terme."""
    import json

    from conftest import EX
    from mathocr.schemas import ProofStructure, ReferenceStatement
    from mathocr.stages.latex2lean import translate_structure

    ref = ReferenceStatement(**json.loads((EX / "exercice.json").read_text()))
    st = json.loads((EX / "fixtures" / "structure.json").read_text())
    for s in st["steps"]:
        if s["id"] in ("S01", "S08"):
            s["statement"] = s["statement"].replace(r"\sum_{k=0}", r"\sum_{h=0}")
    r = translate_structure(ref, ProofStructure(**st))
    assert set(r.incoherent) == {"S01", "S08"} and not r.failed
    assert "variable k non définie" in r.incoherent["S08"]


@pytest.mark.parametrize("latex,lean", [
    # formulations réelles produites par Claude Opus 5.5 lors du premier essai
    (r"P(0) \text{ est vraie et } P(n) \Rightarrow P(n+1)\ \forall n \in \mathbb{N}", "P 0 ∧ (∀ n : ℕ, P n → P (n + 1))"),
    (r"\text{Pour } n=0,\ \sum_{k=0}^{-1}(2k+1) \text{ est vide}", "(∑ k ∈ Finset.range 0, (2 * k + 1) : ℕ) = 0"),
    (r"P(n) \text{ vraie}", "P n"),
])
def test_real_world_phrasings(latex, lean):
    assert T().claim(latex).lean == lean


def test_forall_prefixed_definition():
    name, binders, body, _ = T().definition(r"\forall n \in \mathbb{N},\ P(n) : \sum_{k=0}^{n-1} (2k+1) = n^2")
    assert name == "P" and binders[0].name == "n" and body == "∑ k ∈ Finset.range n, (2 * k + 1) = n ^ 2"


@pytest.mark.parametrize("stmt", [
    # quatre notations réellement produites par Claude Opus 5.5 pour la même définition
    r"P(n) : \sum_{k=0}^{n-1} (2k+1) = n^2",
    r"\forall n \in \mathbb{N},\ P(n) : \sum_{k=0}^{n-1} (2k+1) = n^2",
    r"P(n) : \sum_{k=0}^{n-1} (2k+1) = n^2 \quad (n \in \mathbb{N})",
    r"P(n) \triangleq \sum_{k=0}^{n-1}(2k+1)=n^2",
    r"P(n) \stackrel{\text{def}}{=} \sum_{k=0}^{n-1}(2k+1)=n^2",
    r"P(n) \Leftrightarrow \sum_{k=0}^{n-1}(2k+1)=n^2",
])
def test_definition_notations(stmt):
    name, _, body, _ = T().definition(stmt)
    assert name == "P" and body == "∑ k ∈ Finset.range n, (2 * k + 1) = n ^ 2"
