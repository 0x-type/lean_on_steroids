"""Évaluation numérique d'expressions LaTeX de la copie (via SymPy).

Sert au contrôle de fidélité : on évalue le membre écrit par l'élève et le
membre formalisé en Lean aux mêmes points, puis on compare. SymPy suit la
convention de Karr pour les sommes à bornes inversées : ∑_{k=0}^{-1} = 0,
conforme à la convention « somme vide » de l'énoncé.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

import sympy
from sympy.parsing.latex import parse_latex

_SUM_RE = re.compile(r"\\(sum|prod)\s*_\{[^{}]*\}\s*\^\{[^{}]*\}|\\(sum|prod)\s*_\{[^{}]*\}\s*\^[^{\s]")

REPLACEMENTS = [
    (r"\left", ""), (r"\right", ""), (r"\,", " "), (r"\;", " "), (r"\!", ""), (r"\displaystyle", ""),
    (r"\ell", "l"), (r"\times", r"\cdot"),
]


class LatexEvalError(Exception):
    pass


def _balanced_group(s: str, i: int) -> int | None:
    """Indice juste après le groupe parenthésé commençant en s[i] == '('."""
    if i >= len(s) or s[i] != "(":
        return None
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def preprocess(latex: str) -> str:
    s = latex.strip().strip("$")
    for a, b in REPLACEMENTS:
        s = s.replace(a, b)
    # \sum_{..}^{..} (corps) -> (\sum_{..}^{..} (corps)) : lève l'ambiguïté « ∑ … + terme ».
    out, i = [], 0
    while True:
        m = _SUM_RE.search(s, i)
        if not m:
            out.append(s[i:])
            break
        j = m.end()
        while j < len(s) and s[j] == " ":
            j += 1
        end = _balanced_group(s, j)
        if end is None:
            out.append(s[i:m.end()])
            i = m.end()
            continue
        out.append(s[i:m.start()])
        out.append("(" + s[m.start():end] + ")")
        i = end
    return "".join(out)


_IMPLICIT_MUL = re.compile(r"(?<![\\a-zA-Z])([a-zA-Z0-9}])\s*\(")


def parse(latex: str) -> sympy.Expr:
    """Analyse une expression. En cas d'ambiguïté « n(n+1) » (appel de fonction ou
    produit ?), on retient le produit : les copies n'utilisent pas de fonctions
    anonymes à une lettre dans les calculs."""
    src = preprocess(latex)
    for attempt in (src, _IMPLICIT_MUL.sub(r"\1 \\cdot (", src)):
        try:
            e = parse_latex(attempt, backend="lark")
        except Exception as ex:  # noqa: BLE001
            raise LatexEvalError(f"analyse impossible : {type(ex).__name__}") from ex
        if isinstance(e, sympy.Basic):
            return e
    raise LatexEvalError("expression ambiguë pour l'analyseur")


def free_symbols(latex: str) -> set[str]:
    e = parse(latex)
    names = {str(s) for s in e.free_symbols}
    names |= {type(f).__name__ for f in e.atoms(sympy.core.function.AppliedUndef)}
    return names


@dataclass
class EvalValue:
    value: Fraction | None
    error: str | None = None


def evaluate(latex: str, point: dict[str, str]) -> EvalValue:
    try:
        e = parse(latex)
        subs = {sympy.Symbol(k): sympy.Rational(v) for k, v in point.items()}
        v = sympy.simplify(e.subs(subs).doit())
        if v.free_symbols:
            return EvalValue(None, f"variables non évaluées : {sorted(map(str, v.free_symbols))}")
        if not v.is_rational:
            return EvalValue(None, f"valeur non rationnelle : {v}")
        return EvalValue(Fraction(int(v.p), int(v.q)))
    except LatexEvalError as ex:
        return EvalValue(None, str(ex))
    except Exception as ex:  # noqa: BLE001
        return EvalValue(None, f"évaluation impossible : {type(ex).__name__}")


def parse_lean_value(text: str | None) -> Fraction | None:
    if text is None:
        return None
    t = text.strip()
    try:
        if "/" in t:
            a, b = t.split("/", 1)
            return Fraction(int(a.strip()), int(b.strip()))
        return Fraction(int(t))
    except ValueError:
        return None
