from fractions import Fraction

from mathocr.stages.latexeval import evaluate, free_symbols


def test_sum_and_empty_sum():
    assert evaluate(r"\sum_{k=0}^{n-1} (2k+1)", {"n": "4"}).value == Fraction(16)
    assert evaluate(r"\sum_{k=0}^{-1} (2k+1)", {}).value == 0  # convention somme vide


def test_sum_followed_by_term_is_not_absorbed():
    assert evaluate(r"\sum_{k=0}^{n-1} (2k+1) + (2n+1)", {"n": "3"}).value == Fraction(16)


def test_implicit_product():
    assert evaluate(r"\frac{n(n+1)}{2}", {"n": "4"}).value == Fraction(10)


def test_free_symbols_detect_malformed_readings():
    assert free_symbols(r"\ell k+1") >= {"l"}
    assert "Y" in free_symbols("Y(n)")
    assert free_symbols(r"\sum_{h=0}^{n-1} (2h+1)") == {"n"}
