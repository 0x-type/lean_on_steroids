"""Traduction déterministe LaTeX → Lean, sans modèle de langage.

Pourquoi : la traduction par un LLM coûte cher et peut « réparer » l'élève.
Un traducteur écrit en code ne fait que ce qu'il sait faire, à l'identique
à chaque fois ; ce qu'il ne sait pas traduire est renvoyé à l'agent.

Ce module possède son propre analyseur (descente récursive) qui conserve
l'expression telle qu'écrite — parenthèses comprises, sans simplification.
L'empreinte numérique (`fidelity.py`) évalue ensuite la même formule avec
SymPy : deux analyseurs indépendants doivent donner les mêmes valeurs.

Sémantique :
* domaine de calcul : ℕ s'il n'y a ni soustraction ni division ; ℤ s'il y a une
  soustraction ; ℚ s'il y a une division (les variables sont alors converties).
  On évite ainsi la soustraction tronquée de ℕ, qui changerait le sens.
* ∑_{k=0}^{X-1} ↦ Finset.range X ; ∑_{k=0}^{U} ↦ Finset.range (U + 1) ;
  ∑_{k=a}^{b} ↦ Finset.Icc a b (refusé si la borne contient une soustraction).
* le corps d'une somme est un produit (∑ 2k + 1 = (∑ 2k) + 1), comme en Lean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schemas import Binder, Formalization, ProofStructure, ReferenceStatement, ScopeFormal, Sides, StepFormal


class TranslationError(Exception):
    pass


class IncoherentReading(TranslationError):
    """La formule lue n'a pas de sens (variable non définie…) : c'est la LECTURE qui est en cause.
    On ne l'envoie pas à un agent, qui risquerait de « réparer » la copie ; examen humain."""


# ---------------------------------------------------------------------------
# Analyse lexicale
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|\d+|[A-Za-z]|[^\sA-Za-z\d]")
_DROP = {r"\left", r"\right", r"\,", r"\;", r"\!", r"\ ", r"\displaystyle", r"\quad", r"\qquad", r"\big", r"\Big"}
_GREEK = {r"\alpha": "α", r"\beta": "β", r"\lambda": "λ", r"\mu": "μ", r"\theta": "θ", r"\varepsilon": "ε",
          r"\epsilon": "ε", r"\delta": "δ"}
RELATIONS = {"=": "=", "<": "<", ">": ">", r"\leq": "≤", r"\le": "≤", r"\leqslant": "≤", r"\geq": "≥",
             r"\ge": "≥", r"\geqslant": "≥", r"\neq": "≠", r"\ne": "≠", "≤": "≤", "≥": "≥", "≠": "≠"}
TYPES = {r"\mathbb{N}": "ℕ", r"\mathbb{Z}": "ℤ", r"\mathbb{Q}": "ℚ", r"\mathbb{R}": "ℝ", "ℕ": "ℕ", "ℤ": "ℤ",
         "ℚ": "ℚ", "ℝ": "ℝ", "IN": "ℕ", r"\N": "ℕ", r"\Z": "ℤ", r"\R": "ℝ", r"\Q": "ℚ"}
DOMAIN_RANK = {"ℕ": 0, "ℤ": 1, "ℚ": 2, "ℝ": 3}


def tokenize(s: str) -> list[str]:
    s = s.replace(r"\mathbb N", r"\mathbb{N}")
    out = []
    for t in _TOKEN.findall(s):
        if t in _DROP:
            continue
        out.append(_GREEK.get(t, t))
    return out


# ---------------------------------------------------------------------------
# Arbre syntaxique
# ---------------------------------------------------------------------------


@dataclass
class Num:
    v: str


@dataclass
class Var:
    name: str


@dataclass
class Paren:
    e: object


@dataclass
class Bin:
    op: str  # + - * /
    a: object
    b: object


@dataclass
class Neg:
    e: object


@dataclass
class Pow:
    base: object
    exp: object


@dataclass
class Fact:
    e: object


@dataclass
class Binom:
    n: object
    k: object


@dataclass
class BigOp:
    op: str  # sum | prod
    var: str
    lo: object
    hi: object
    body: object


@dataclass
class Pred:
    name: str
    arg: object


class Parser:
    """Expressions arithmétiques d'une copie : + - · × / ^ ! \\frac \\binom \\sum \\prod, produit implicite."""

    def __init__(self, toks: list[str], preds: set[str]):
        self.t = toks
        self.i = 0
        self.preds = preds

    def peek(self, k: int = 0) -> str | None:
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (expected is not None and tok != expected):
            raise TranslationError(f"attendu « {expected} », trouvé « {tok} »")
        self.i += 1
        return tok

    def done(self) -> bool:
        return self.i >= len(self.t)

    # expr := term (('+'|'-') term)*
    def expr(self):
        e = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            e = Bin(op, e, self.term())
        return e

    def _starts_factor(self, tok: str | None) -> bool:
        if tok is None:
            return False
        return (tok in ("(", "{", r"\frac", r"\dfrac", r"\tfrac", r"\binom", r"\sum", r"\prod")
                or re.fullmatch(r"[A-Za-zα-ω]", tok) is not None)

    # term := factor (('*'|\cdot|\times|'/'|implicite) factor)*
    def term(self):
        e = self.factor()
        while True:
            tok = self.peek()
            if tok in ("*", r"\cdot", r"\times"):
                self.take()
                e = Bin("*", e, self.factor())
            elif tok in ("/", r"\div"):
                self.take()
                e = Bin("/", e, self.factor())
            elif self._starts_factor(tok):
                e = Bin("*", e, self.factor())
            else:
                return e

    def factor(self):
        if self.peek() == "-":
            self.take()
            return Neg(self.factor())
        if self.peek() == "+":
            self.take()
            return self.factor()
        return self.power()

    def power(self):
        base = self.postfix()
        if self.peek() == "^":
            self.take()
            return Pow(base, self.script())
        return base

    def postfix(self):
        e = self.atom()
        while self.peek() == "!":
            self.take()
            e = Fact(e)
        return e

    def script(self):
        """Argument d'un ^ ou d'un _ : groupe {…} ou un seul jeton."""
        if self.peek() == "{":
            return self.group()
        tok = self.take()
        if tok.isdigit():
            return Num(tok[0]) if len(tok) > 1 and self._split_digits(tok) else Num(tok)
        if re.fullmatch(r"[A-Za-zα-ω]", tok):
            return Var(tok)
        raise TranslationError(f"exposant ou indice inattendu « {tok} »")

    def _split_digits(self, tok: str) -> bool:
        # « n^23 » en LaTeX signifie n^2 suivi de 3 : on remet le reste dans le flux.
        self.t.insert(self.i, tok[1:])
        return True

    def group(self):
        self.take("{")
        e = self.expr()
        self.take("}")
        return e

    def atom(self):
        tok = self.peek()
        if tok is None:
            raise TranslationError("expression incomplète")
        if tok.isdigit():
            self.take()
            return Num(tok)
        if tok == "(":
            self.take()
            e = self.expr()
            self.take(")")
            return Paren(e)
        if tok == "{":
            return Paren(self.group())
        if tok in (r"\frac", r"\dfrac", r"\tfrac"):
            self.take()
            a = self.group()
            b = self.group()
            return Paren(Bin("/", Paren(a), Paren(b)))
        if tok == r"\binom":
            self.take()
            return Binom(self.group(), self.group())
        if tok in (r"\sum", r"\prod"):
            self.take()
            self.take("_")
            self.take("{")
            var = self.take()
            if not re.fullmatch(r"[A-Za-z]", var):
                raise TranslationError("indice de somme invalide")
            self.take("=")
            lo = self.expr()
            self.take("}")
            self.take("^")
            hi = self.script()
            return BigOp("sum" if tok == r"\sum" else "prod", var, lo, hi, self.term())
        if re.fullmatch(r"[A-Za-zα-ω]", tok):
            self.take()
            if tok in self.preds and self.peek() == "(":
                self.take("(")
                arg = self.expr()
                self.take(")")
                return Pred(tok, arg)
            return Var(tok)
        raise TranslationError(f"symbole non pris en charge « {tok} »")


# ---------------------------------------------------------------------------
# Impression Lean
# ---------------------------------------------------------------------------


def _has(e, kind) -> bool:
    if isinstance(e, kind):
        return True
    if isinstance(e, Bin) and kind is Bin:
        return True
    for v in getattr(e, "__dict__", {}).values():
        if isinstance(v, (Num, Var, Paren, Bin, Neg, Pow, Fact, Binom, BigOp, Pred)) and _has(v, kind):
            return True
    return False


def _uses_op(e, ops: set[str], *, skip_bounds: bool = True) -> bool:
    if isinstance(e, Bin) and e.op in ops:
        return True
    if isinstance(e, Neg) and "-" in ops:
        return True
    if isinstance(e, BigOp):
        return _uses_op(e.body, ops) or (not skip_bounds and (_uses_op(e.lo, ops) or _uses_op(e.hi, ops)))
    if isinstance(e, Pow):
        return _uses_op(e.base, ops)  # l'exposant est traité à part (toujours dans ℕ)
    return any(_uses_op(v, ops, skip_bounds=skip_bounds) for v in getattr(e, "__dict__", {}).values()
               if isinstance(v, (Num, Var, Paren, Bin, Neg, Pow, Fact, Binom, BigOp, Pred)))


def free_vars(e, bound: frozenset = frozenset()) -> set[str]:
    if isinstance(e, Var):
        return set() if e.name in bound else {e.name}
    if isinstance(e, BigOp):
        return free_vars(e.lo, bound) | free_vars(e.hi, bound) | free_vars(e.body, bound | {e.var})
    out: set[str] = set()
    for v in getattr(e, "__dict__", {}).values():
        if isinstance(v, (Num, Var, Paren, Bin, Neg, Pow, Fact, Binom, BigOp, Pred)):
            out |= free_vars(v, bound)
    return out


@dataclass
class Printer:
    domain: str  # type des valeurs : ℕ, ℤ, ℚ, ℝ
    var_types: dict[str, str]
    bound: set[str] = field(default_factory=set)

    def _cast(self, name: str) -> str:
        t = self.var_types.get(name, "ℕ")
        if name in self.bound:
            t = "ℕ"
        if DOMAIN_RANK.get(t, 0) >= DOMAIN_RANK[self.domain]:
            return name
        return f"({name} : {self.domain})"

    def nat(self, e) -> str:
        """Expression d'indice/borne/exposant : doit rester dans ℕ, sans soustraction."""
        if _uses_op(e, {"-", "/"}, skip_bounds=False):
            raise TranslationError("soustraction ou division dans une borne ou un exposant (sémantique de ℕ)")
        return Printer("ℕ", self.var_types, self.bound).p(e)

    def p(self, e, ctx: int = 0) -> str:
        # ctx : 0 = sommet, 1 = opérande de + -, 2 = opérande de * /, 3 = base de ^
        if isinstance(e, Num):
            return e.v
        if isinstance(e, Var):
            return self._cast(e.name)
        if isinstance(e, Paren):
            return f"({self.p(e.e)})"
        if isinstance(e, Neg):
            s = f"-{self.p(e.e, 3)}"
            return f"({s})" if ctx >= 2 else s
        if isinstance(e, Bin):
            if e.op in ("+", "-"):
                s = f"{self.p(e.a, 1)} {e.op} {self.p(e.b, 2 if e.op == '-' else 1)}"
                return f"({s})" if ctx >= 2 else s
            s = f"{self.p(e.a, 2)} {e.op} {self.p(e.b, 3)}"
            return f"({s})" if ctx >= 3 else s
        if isinstance(e, Pow):
            return f"{self.p(e.base, 3)} ^ {self.nat_atom(e.exp)}"
        if isinstance(e, (Fact, Binom)):
            inner = (f"Nat.factorial {self.nat_atom(e.e)}" if isinstance(e, Fact)
                     else f"Nat.choose {self.nat_atom(e.n)} {self.nat_atom(e.k)}")
            if self.domain != "ℕ":
                return f"(({inner} : ℕ) : {self.domain})"
            return f"({inner})" if ctx >= 2 else inner
        if isinstance(e, BigOp):
            sym = "∑" if e.op == "sum" else "∏"
            rng = self._range(e)
            inner = Printer(self.domain, self.var_types, self.bound | {e.var})
            body = inner.p(e.body, 3)
            s = f"{sym} {e.var} ∈ {rng}, {body}"
            return f"({s})" if ctx >= 1 else s
        if isinstance(e, Pred):
            raise TranslationError("prédicat à l'intérieur d'un calcul")
        raise TranslationError(f"nœud inconnu {type(e).__name__}")

    def nat_atom(self, e) -> str:
        s = self.nat(e)
        return s if re.fullmatch(r"[\w']+", s) else f"({s})"

    def _range(self, e: BigOp) -> str:
        lo, hi = e.lo, e.hi
        if isinstance(lo, Num) and lo.v == "0":
            # ∑_{k=0}^{X-1}  et  ∑_{k=0}^{-1}
            if isinstance(hi, Bin) and hi.op == "-" and isinstance(hi.b, Num) and hi.b.v == "1":
                return f"Finset.range {self.nat_atom(hi.a)}"
            if isinstance(hi, Neg) and isinstance(hi.e, Num) and hi.e.v == "1":
                return "Finset.range 0"
            return f"Finset.range ({self.nat(hi)} + 1)"
        return f"Finset.Icc {self.nat_atom(lo)} {self.nat_atom(hi)}"


def domain_of(e, var_types: dict[str, str]) -> str:
    d = "ℕ"
    for v in free_vars(e):
        t = var_types.get(v, "ℕ")
        if DOMAIN_RANK.get(t, 0) > DOMAIN_RANK[d]:
            d = t
    if _uses_op(e, {"/"}) and DOMAIN_RANK[d] < DOMAIN_RANK["ℚ"]:
        d = "ℚ"
    elif _uses_op(e, {"-"}) and DOMAIN_RANK[d] < DOMAIN_RANK["ℤ"]:
        d = "ℤ"
    return d


# ---------------------------------------------------------------------------
# Affirmations : relations, prédicats, connecteurs logiques
# ---------------------------------------------------------------------------

_TEXT = re.compile(r"\\text\{([^}]*)\}|\\textrm\{([^}]*)\}|\\mathrm\{([^}]*)\}")
TRUE_WORDS = ("est vraie", "est vérifiée", "est vrai", "est satisfaite", "vraie", "vérifiée")
ZERO_WORDS = ("est nulle", "est nul", "est vide", "vaut 0", "vaut zéro")


@dataclass
class Claim:
    lean: str
    sides: Sides | None = None
    free: set[str] = field(default_factory=set)


def _clean(s: str) -> str:
    s = s.strip().strip("$").strip()
    s = _TEXT.sub(lambda m: " " + next(g for g in m.groups() if g is not None).strip() + " ", s)
    s = s.replace("\\ ", " ").replace("\\,", " ").replace("\\;", " ").replace("\\quad", " ")
    return re.sub(r"\s+", " ", s).strip().rstrip(".").strip()


def _split_top(s: str, seps: list[str], *, stop_at_forall: bool = False) -> tuple[str, str, str] | None:
    """Coupe s au premier séparateur de plus haut niveau (hors parenthèses/accolades).

    Avec `stop_at_forall`, la recherche s'arrête au premier ∀ : sa portée s'étend
    jusqu'au bout de la phrase (« A et ∀n, B ⇒ C » = A ∧ (∀n, B → C))."""
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if stop_at_forall and depth == 0 and s.startswith(r"\forall", i):
            return None
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        elif depth == 0:
            for sep in seps:
                if s.startswith(sep, i):
                    if sep.startswith("\\") and i + len(sep) < len(s) and s[i + len(sep)].isalpha():
                        continue
                    return s[:i].strip(), sep, s[i + len(sep):].strip()
        i += 1
    return None


_TRAILING_DOMAIN = re.compile(
    r"\s*,?\s*\(\s*(?:\\forall\s*)?([A-Za-z])\s*\\in\s*(\\mathbb\{[NZQR]\}|ℕ|ℤ|ℚ|ℝ)\s*\)\s*$")
_DEF_SYMBOLS = [r"\\(?:stackrel|overset)\{\s*(?:\\text\{)?\s*d[ée]f\s*\}?\s*\}\{=\}", r"\\coloneqq", r"\\triangleq",
                r"\\colon", r"\\dot\{=\}", r"\\doteq", r"\\equiv", r"\\iff", r"\\Leftrightarrow", r"\\Longleftrightarrow", r":\\Leftrightarrow",
                r":\\iff", r":=", r"≜", r"≡", r"⇔", r":", r"="]
_DEF_HEAD = re.compile(r"([A-Z])\s*\(\s*([A-Za-z])\s*\)\s*(?:" + "|".join(_DEF_SYMBOLS) + r")\s*(.*)$")


class Translator:
    def __init__(self, var_types: dict[str, str], preds: set[str]):
        self.var_types = dict(var_types)
        self.preds = preds

    def expr_ast(self, latex: str):
        p = Parser(tokenize(latex), self.preds)
        e = p.expr()
        if not p.done():
            raise TranslationError(f"reste non analysé : « {' '.join(p.t[p.i:])} »")
        return e

    def claim(self, latex: str) -> Claim:
        s = _clean(latex)
        if not s:
            raise TranslationError("affirmation vide")
        # « Pour n = 0, C » : contexte du cas traité. Sans effet si n n'apparaît pas dans C ;
        # sinon on laisse l'agent formaliser (substitution à faire).
        m = re.match(r"^(?:Pour|pour|Si|si|Lorsque|lorsque)\s+([A-Za-z])\s*=\s*([^,]+),\s*(.+)$", s)
        if m:
            inner = self.claim(m.group(3))
            if m.group(1) in inner.free:
                raise TranslationError(f"affirmation conditionnée par {m.group(1)} = {m.group(2)}")
            return inner
        # ∀ x, A   |   ∀ x ∈ E, A
        m = re.match(r"^\\forall\s*([A-Za-z])\s*(?:\\in\s*(\\mathbb\{[NZQR]\}|ℕ|ℤ|ℚ|ℝ))?\s*,?\s*(.*)$", s)
        if m:
            v, typ, rest = m.group(1), m.group(2), m.group(3)
            t = TYPES.get(typ, self.var_types.get(v, "ℕ")) if typ else self.var_types.get(v, "ℕ")
            saved = self.var_types.get(v)
            self.var_types[v] = t
            inner = self.claim(rest)
            if saved is None:
                self.var_types.pop(v, None)
            return Claim(f"∀ {v} : {t}, {inner.lean}", None, inner.free - {v})
        # « … (n ∈ ℕ) » en fin d'affirmation : équivaut à « … ∀ n ∈ ℕ ».
        dm = _TRAILING_DOMAIN.search(s)
        if dm:
            s = s[: dm.start()].strip() + rf" \forall {dm.group(1)} \in {dm.group(2)}"
        # « et » en toutes lettres sépare deux affirmations : « P(0) est vraie et P(n) ⇒ P(n+1) ∀n »
        # = P(0) ∧ (∀n, P(n) ⇒ P(n+1)). Il se coupe avant ⇒ et avant un ∀ final.
        parts = _split_top(s, [" et "], stop_at_forall=True)
        if parts:
            a, _, b = parts
            ca, cb = self.claim(a), self.claim(b)
            return Claim(f"{_wrap(ca.lean)} ∧ {_wrap(cb.lean)}", None, ca.free | cb.free)
        # « A ∀ n ∈ ℕ » en fin de phrase (fréquent dans les copies)
        m = re.match(r"^(.*?)\s*\\forall\s*([A-Za-z])\s*(?:\\in\s*(\\mathbb\{[NZQR]\}|ℕ|ℤ|ℚ|ℝ))?\s*$", s)
        if m and m.group(1):
            return self.claim(rf"\forall {m.group(2)}" + (rf" \in {m.group(3)}" if m.group(3) else "") + ", " + m.group(1))
        for seps, lean_op in (([r"\iff", r"\Leftrightarrow", "⇔"], "↔"),
                              ([r"\Rightarrow", r"\implies", "⇒"], "→")):
            parts = _split_top(s, seps, stop_at_forall=True)
            if parts:
                a, _, b = parts
                ca, cb = self.claim(a), self.claim(b)
                return Claim(f"({ca.lean}) {lean_op} ({cb.lean})" if lean_op == "↔" else f"{ca.lean} → {cb.lean}",
                             None, ca.free | cb.free)
        parts = _split_top(s, [r"\land", r"\wedge"], stop_at_forall=True)
        if parts:
            a, _, b = parts
            ca, cb = self.claim(a), self.claim(b)
            return Claim(f"{_wrap(ca.lean)} ∧ {_wrap(cb.lean)}", None, ca.free | cb.free)
        low = s.lower()
        for w in TRUE_WORDS:
            if low.endswith(" " + w) or low == w:
                return self.claim(s[: -len(w)].strip())
        for w in ZERO_WORDS:
            if low.endswith(" " + w):
                return self.claim(s[: -len(w)].strip() + " = 0")
        if re.search(r"[A-Za-zÀ-ÿ]{3,}", re.sub(r"\\[A-Za-z]+", "", s)):
            raise TranslationError(f"texte non mathématique : « {s} »")
        # Prédicat seul : P(t)
        m = re.fullmatch(r"([A-Z])\s*\((.*)\)", s)
        if m and m.group(1) in self.preds:
            arg = self.expr_ast(m.group(2))
            d = domain_of(arg, self.var_types)
            if d != "ℕ" and self.var_types.get(next(iter(free_vars(arg)), ""), "ℕ") == "ℕ" and _uses_op(arg, {"-", "/"}):
                raise TranslationError("argument de prédicat hors de ℕ")
            pr = Printer("ℕ", self.var_types)
            a = pr.p(arg)
            lean_arg = a if re.fullmatch(r"[\w']+", a) else f"({a})"
            return Claim(f"{m.group(1)} {lean_arg}", None, free_vars(arg))
        # Relation
        rel = None
        for tok in sorted(RELATIONS, key=len, reverse=True):
            parts = _split_top(s, [tok])
            if parts and (rel is None or len(parts[0]) < len(rel[0])):
                rel = parts
        if rel is None:
            raise TranslationError(f"ni relation ni prédicat : « {s} »")
        lhs_l, op, rhs_l = rel
        if any(_split_top(rhs_l, [t]) for t in RELATIONS):
            raise TranslationError("plusieurs relations dans une même étape (chaîne non découpée)")
        a, b = self.expr_ast(lhs_l), self.expr_ast(rhs_l)
        d = max(domain_of(a, self.var_types), domain_of(b, self.var_types), key=lambda t: DOMAIN_RANK[t])
        pr = Printer(d, self.var_types)
        la, lb = pr.p(a), pr.p(b)
        if d != "ℕ" or (not free_vars(a) and not free_vars(b)):
            # Domaine explicite : sinon Lean peut calculer dans ℕ (soustraction tronquée) quand les
            # variables n'apparaissent qu'en exposant ou en borne (ex. 2^{n+1} - 1).
            la = f"({la} : {d})"
        fv = free_vars(a) | free_vars(b)
        sides = Sides(relation=RELATIONS[op], lhs_lean=la, rhs_lean=lb, lhs_latex=lhs_l, rhs_latex=rhs_l,
                      value_type=d, variables=[Binder(name=v, type=self.var_types.get(v, "ℕ")) for v in sorted(fv)])
        return Claim(f"{la} {RELATIONS[op]} {lb}", sides, fv)

    def definition(self, latex: str) -> tuple[str, list[Binder], str, Sides | None]:
        s = _clean(latex)
        # « ∀ n ∈ ℕ, P(n) : … » : le quantificateur porte sur la variable de la définition.
        s = re.sub(r"^\\forall\s*[A-Za-z]\s*(?:\\in\s*(?:\\mathbb\{[NZQR]\}|ℕ|ℤ|ℚ|ℝ))?\s*,\s*", "", s)
        # « P(n) : … (n ∈ ℕ) » : domaine de la variable indiqué en fin de définition.
        dm = _TRAILING_DOMAIN.search(s)
        if dm:
            self.var_types[dm.group(1)] = TYPES[dm.group(2)]
            s = s[: dm.start()].strip()
        m = _DEF_HEAD.match(s)
        if not m:
            raise TranslationError("définition non reconnue (attendu « P(n) : … »)")
        name, var, body = m.group(1), m.group(2), m.group(3)
        c = self.claim(body)
        loose = sorted(c.free - {var})
        if loose:
            raise IncoherentReading(f"lecture incohérente : variable {', '.join(loose)} non définie dans la "
                                    f"définition de {name} (indice de somme mal relu ?)")
        return name, [Binder(name=var, type=self.var_types.get(var, "ℕ"))], c.lean, c.sides


def _wrap(s: str) -> str:
    return f"({s})" if any(op in s for op in (" → ", " ↔ ", "∀")) else s


# ---------------------------------------------------------------------------
# Formalisation d'une structure complète
# ---------------------------------------------------------------------------


def variable_types(ref: ReferenceStatement, st: ProofStructure) -> dict[str, str]:
    types: dict[str, str] = {}
    for m in re.finditer(r"∀\s*\(?\s*([A-Za-z])\s*:\s*(ℕ|ℤ|ℚ|ℝ)", ref.lean_statement):
        types.setdefault(m.group(1), m.group(2))
    for s in st.steps:
        for m in re.finditer(r"([A-Za-z])\s*\\in\s*(\\mathbb\{[NZQR]\}|ℕ|ℤ|ℚ|ℝ)", s.statement):
            types.setdefault(m.group(1), TYPES[m.group(2)])
    for sc in st.scopes:
        for v in sc.variables:
            types.setdefault(v, "ℕ")
    return types


@dataclass
class TranslationResult:
    formalization: Formalization
    failed: dict[str, str]  # étape -> raison (traduisible par un agent)
    incoherent: dict[str, str] = field(default_factory=dict)  # étape -> raison (lecture à revoir, pas d'agent)


NOT_FORMALIZED = {"introduction": "introduction (pas une affirmation)", "annonce": "annonce du but",
                  "commentaire": "commentaire"}


def translate_structure(ref: ReferenceStatement, st: ProofStructure) -> TranslationResult:
    types = variable_types(ref, st)
    preds = {m.group(1) for s in st.steps if s.kind == "definition"
             for m in [_DEF_HEAD.search(_clean(s.statement))] if m}
    tr = Translator(types, preds)
    scope_vars = {sc.id: sc.variables for sc in st.scopes}
    scopes = [ScopeFormal(scope_id=sc.id, binders=[Binder(name=v, type=types.get(v, "ℕ")) for v in sc.variables])
              for sc in st.scopes if sc.id != "global" and sc.variables]
    steps: list[StepFormal] = []
    failed: dict[str, str] = {}
    incoherent: dict[str, str] = {}
    for s in st.steps:
        if s.kind in NOT_FORMALIZED:
            steps.append(StepFormal(step_id=s.id, role="none", not_formalized_reason=NOT_FORMALIZED[s.kind],
                                    origin="code"))
            continue
        uses = [d for d in s.depends_on if any(x.id == d and x.kind != "definition" for x in st.steps)]
        try:
            if s.kind == "definition":
                name, binders, body, sides = tr.definition(s.statement)
                steps.append(StepFormal(step_id=s.id, role="def", def_name=name, def_binders=binders,
                                        def_body=body, sides=sides, origin="code"))
                continue
            c = tr.claim(s.statement)
            bound_here = set()
            sc = s.scope
            while sc and sc != "global":
                bound_here |= set(scope_vars.get(sc, []))
                sc = next((x.parent for x in st.scopes if x.id == sc), None)
            extra = sorted(c.free - bound_here)
            unknown = [v for v in extra if v not in types]
            if unknown:
                raise IncoherentReading(
                    f"lecture incohérente : variable {', '.join(unknown)} non définie dans la copie "
                    f"(indice de somme mal relu ?)")
            lean = c.lean
            if extra:
                lean = f"∀ {' '.join(f'({v} : {types.get(v, chr(8469))})' for v in extra)}, {lean}"
            pred = None
            m = re.fullmatch(r"([A-Z]) (\S+|\(.*\))", c.lean)
            if m and m.group(1) in preds:
                arg_l = re.search(rf"{m.group(1)}\s*\(([^()]*)\)", _clean(s.statement))
                pred = (m.group(1), arg_l.group(1) if arg_l else "")
            if s.kind == "hypothese":
                steps.append(StepFormal(step_id=s.id, role="hyp", claim=lean, predicate_app=pred, sides=c.sides,
                                        origin="code"))
            else:
                steps.append(StepFormal(step_id=s.id, role="prop", claim=lean, uses=uses, sides=c.sides,
                                        predicate_app=pred, origin="code"))
        except IncoherentReading as ex:
            incoherent[s.id] = str(ex)
            steps.append(StepFormal(step_id=s.id, role="none", not_formalized_reason=str(ex), origin="code"))
        except TranslationError as ex:
            failed[s.id] = str(ex)
    return TranslationResult(Formalization(scopes=scopes, steps=steps,
                                           provenance="traduction déterministe LaTeX → Lean (sans LLM)"),
                             failed, incoherent)
