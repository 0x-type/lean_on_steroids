"""Génération déterministe du fichier Lean à partir de la structure et de la formalisation.

Seuls des *fragments* (termes, corps de définitions, preuves proposées par un
agent) viennent des agents ; tout le reste du fichier — signatures, preuves
élémentaires, assemblage, sondes de réfutation, empreintes numériques,
contrôle des axiomes — est produit ici, à partir d'un gabarit de confiance.

Principe de fidélité : chaque étape écrite par l'élève devient *un* théorème
dont les seules hypothèses sont les dépendances déclarées de l'étape. La
preuve de chaque théorème est l'automatisation élémentaire `mathocr_core`
(fichier `MathOCRCheck/Auto.lean`) : l'agent ne peut donc pas « réparer »
silencieusement une étape en y glissant un argument absent de la copie. Si
un agent propose une preuve (niveau 2), elle est compilée séparément et le
statut de l'étape le signale explicitement.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..schemas import Binder, Formalization, PolicyViolation, ProofStructure, ReferenceStatement, StepFormal
from .policy import check_fragment, check_identifier

NS = "Copie"
SAMPLE_VALUES = {
    "ℕ": ["0", "1", "2", "3", "4", "5", "7"],
    "Nat": ["0", "1", "2", "3", "4", "5", "7"],
    "ℤ": ["0", "1", "2", "3", "-1", "-2", "5"],
    "Int": ["0", "1", "2", "3", "-1", "-2", "5"],
    "ℚ": ["0", "1", "2", "-1", "1/2", "3", "-3/2"],
    "ℝ": ["0", "1", "2", "-1", "1/2", "3", "-3/2"],
}
EVAL_TYPES = {"ℕ", "Nat", "ℤ", "Int", "ℚ", "Rat"}
MAX_POINTS = 12


@dataclass
class Segment:
    kind: str  # def | step | agent_proof | agent_refutation | sans_dep | refutation | assembly | accord | eval | axioms
    step_id: str | None
    decl: str | None
    start: int
    end: int
    meta: dict = field(default_factory=dict)


@dataclass
class GeneratedLean:
    source: str
    segments: list[Segment]
    violations: list[PolicyViolation]
    theorem_decls: list[str]


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.segments: list[Segment] = []

    @property
    def next_line(self) -> int:
        return len(self.lines) + 1

    def emit(self, text: str = "") -> None:
        self.lines.extend(text.split("\n"))

    def block(self, kind: str, step_id: str | None, decl: str | None, text: str, **meta) -> None:
        start = self.next_line
        self.emit(text)
        self.segments.append(Segment(kind, step_id, decl, start, self.next_line - 1, meta))


def _binders_src(binders: list[Binder]) -> str:
    return " ".join(f"({b.name} : {b.type})" for b in binders)


def _doc(text: str) -> str:
    return text.replace("-/", "- /").replace("/-", "/ -")


class LeanGenerator:
    def __init__(self, ref: ReferenceStatement, structure: ProofStructure, formal: Formalization):
        self.ref = ref
        self.st = structure
        self.fm = formal
        self.violations: list[PolicyViolation] = []
        self.scope_binders = {s.scope_id: s.binders for s in formal.scopes}
        self.scopes = {s.id: s for s in structure.scopes}
        self.defs = [f.def_name for f in formal.steps if f.role == "def" and f.def_name]
        self._validate()

    # -- validation des fragments ------------------------------------------------

    def _validate(self) -> None:
        v = self.violations
        v += check_fragment(self.ref.lean_statement, "énoncé de référence")
        for sc in self.fm.scopes:
            for b in sc.binders:
                v += check_identifier(b.name, f"portée {sc.scope_id}")
                v += check_fragment(b.type, f"portée {sc.scope_id}")
        for f in self.fm.steps:
            w = f"étape {f.step_id}"
            if f.role == "def":
                v += check_identifier(f.def_name or "", w)
                v += check_fragment(f.def_body or "", w)
                for b in f.def_binders:
                    v += check_identifier(b.name, w)
                    v += check_fragment(b.type, w)
            if f.claim:
                v += check_fragment(f.claim, w)
            if f.sides:
                v += check_fragment(f.sides.lhs_lean, w + " (membre gauche)")
                v += check_fragment(f.sides.rhs_lean, w + " (membre droit)")
                for b in f.sides.variables:
                    v += check_identifier(b.name, w)
                    v += check_fragment(b.type, w)
            if f.agent_proof:
                v += check_fragment(f.agent_proof, w + " (preuve agent)", multiline=True)
            if f.agent_refutation:
                v += check_fragment(f.agent_refutation, w + " (réfutation agent)", multiline=True)
        known = {s.id for s in self.st.steps}
        for f in self.fm.steps:
            if f.step_id not in known:
                v.append(PolicyViolation(where=f"étape {f.step_id}", token=f.step_id,
                                         detail="étape Lean sans étape correspondante dans la copie"))
            for u in f.uses:
                if u not in known:
                    v.append(PolicyViolation(where=f"étape {f.step_id}", token=u, detail="dépendance inconnue"))

    # -- portées -------------------------------------------------------------------

    def _scope_chain(self, scope_id: str) -> list[str]:
        chain = []
        cur: str | None = scope_id
        while cur and cur != "global":
            chain.append(cur)
            cur = self.scopes[cur].parent if cur in self.scopes else None
        return list(reversed(chain))

    def _vars(self, scope_id: str) -> list[Binder]:
        out: list[Binder] = []
        for sc in self._scope_chain(scope_id):
            out += self.scope_binders.get(sc, [])
        return out

    def _assumptions(self, scope_id: str) -> list[str]:
        return self.scopes[scope_id].assumptions if scope_id in self.scopes else []

    def _formal(self, sid: str) -> StepFormal | None:
        try:
            return self.fm.of(sid)
        except KeyError:
            return None

    def _is_ancestor(self, anc: str, scope: str) -> bool:
        return anc == "global" or anc in self._scope_chain(scope)

    def _dep_type(self, dep: str, at_scope: str) -> str:
        """Type de l'hypothèse représentant l'étape `dep` vue depuis la portée `at_scope`."""
        dstep = self.st.step(dep)
        f = self._formal(dep)
        claim = f.claim if f else "True"
        if self._is_ancestor(dstep.scope, at_scope):
            return claim
        # Étape d'une autre portée : on la « décharge » (∀ variables, hypothèses → énoncé).
        own = set(b.name for b in self._vars(at_scope))
        vs = [b for b in self._vars(dstep.scope) if b.name not in own]
        hyps = []
        for a in self._assumptions(dstep.scope):
            fa = self._formal(a)
            if fa and fa.claim:
                hyps.append(f"(h_{a} : {fa.claim})")
        bind = " ".join([_binders_src(vs)] + hyps).strip()
        return f"∀ {bind}, {claim}" if bind else claim

    def _step_binders(self, sid: str) -> list[tuple[str, str]]:
        step = self.st.step(sid)
        f = self._formal(sid)
        out = [(b.name, b.type) for b in self._vars(step.scope)]
        for u in (f.uses if f else []):
            uf = self._formal(u)
            if uf is None or uf.role in ("def", "none"):
                continue
            out.append((f"h_{u}", self._dep_type(u, step.scope)))
        return out

    # -- termes d'assemblage -------------------------------------------------------------

    def term(self, sid: str, at_scope: str) -> str:
        """Terme Lean prouvant l'étape `sid` dans la portée `at_scope` en composant les théorèmes."""
        step = self.st.step(sid)
        f = self._formal(sid)
        if f is None:
            return "sorry_missing_formalization"
        if f.role == "hyp":
            return f"h_{sid}"
        if not self._is_ancestor(step.scope, at_scope):
            own = set(b.name for b in self._vars(at_scope))
            vs = [b.name for b in self._vars(step.scope) if b.name not in own]
            hs = [f"h_{a}" for a in self._assumptions(step.scope)]
            inner = self.term(sid, step.scope)
            params = " ".join(vs + hs)
            return f"(fun {params} => {inner})" if params else inner
        args = [b.name for b in self._vars(step.scope)]
        for u in f.uses:
            uf = self._formal(u)
            if uf is None or uf.role in ("def", "none"):
                continue
            args.append(self.term(u, step.scope))
        return f"({NS}.{sid} {' '.join(args)})" if args else f"{NS}.{sid}"

    # -- preuves ---------------------------------------------------------------------

    def _unfold(self) -> str:
        return f"simp only [{', '.join(self.defs)}] at *" if self.defs else ""

    def _elementary_proof(self) -> str:
        alts = ["mathocr_core", "(intros; mathocr_core)"]
        if self.defs:
            u = self._unfold()
            alts += [f"({u}; mathocr_core)", f"(intros; {u}; mathocr_core)"]
        return "by\n  first\n  | " + "\n  | ".join(alts)

    def _induction_cases(self, sid: str) -> tuple[str, list[str], list[str]]:
        f = self.fm.of(sid)
        hs = [f"h_{u}" for u in f.uses if (self._formal(u) and self._formal(u).role not in ("def", "none"))]
        var = self.st.pattern.variable or "n"
        zero = [f"exact {h}" for h in hs] + [f"exact {h}.1" for h in hs] + ["mathocr_core"]
        succ = [f"exact {h} {var} ih" for h in hs] + [f"exact {h}.2 {var} ih" for h in hs] + ["mathocr_core"]
        if self.defs:
            zero.append(f"({self._unfold()}; mathocr_core)")
            succ.append(f"({self._unfold()}; mathocr_core)")
        return var, zero, succ

    def _induction_proof(self, sid: str) -> str:
        var, zero, succ = self._induction_cases(sid)
        return (
            f"by\n  intro {var}\n  induction {var} with\n"
            f"  | zero => first | " + " | ".join(zero) + "\n"
            f"  | succ {var} ih => first | " + " | ".join(succ)
        )

    def _induction_inline(self, sid: str) -> str:
        """Même gabarit sur une ligne, chaque cas entre parenthèses (sinon `| succ` serait lu
        comme une alternative du `first` précédent)."""
        var, zero, succ = self._induction_cases(sid)
        return (f"((intro {var}; induction {var} with | zero => (first | {' | '.join(zero)}) "
                f"| succ {var} ih => (first | {' | '.join(succ)})); "
                f"trace \"mathocr:closed_by=schema_recurrence\")")

    def _refutation_proof(self, sid: str) -> tuple[str, str] | None:
        """(énoncé nié, preuve) : tente des contre-exemples aux points d'échantillonnage."""
        f = self.fm.of(sid)
        binders = self._step_binders(sid)
        claim = f.claim
        vars_ = [(n, t) for n, t in binders if not n.startswith("h_")]
        hyps = [(n, t) for n, t in binders if n.startswith("h_")]
        if any(t not in SAMPLE_VALUES for _, t in vars_):
            return None
        bind = " ".join(f"({n} : {t})" for n, t in binders)
        stmt = f"¬ (∀ {bind}, {claim})" if bind else f"¬ ({claim})"
        unfold_hc = f"(try simp only [{', '.join(self.defs)}] at hc); " if self.defs else ""
        discharge = "(by first | mathocr_core" + (f" | ({self._unfold()}; mathocr_core)" if self.defs else "") + ")"
        if not vars_:
            args = " ".join(discharge for _ in hyps)
            head = f"have hc := h {args}; " if bind else ""
            intro = "intro h\n  " if bind else "intro hc\n  "
            return stmt, f"by\n  {intro}first\n  | ({head}{unfold_hc}mathocr_refute_at hc; trace \"mathocr:cex=(clos)\")"
        points = list(itertools.product(*[SAMPLE_VALUES[t] for _, t in vars_]))[:MAX_POINTS]
        alts = []
        for pt in points:
            args = " ".join([f"({v} : {t})" for v, (_, t) in zip(pt, vars_)] + [discharge for _ in hyps])
            label = ", ".join(f"{n} := {v}" for (n, _), v in zip(vars_, pt))
            alts.append(f"(have hc := h {args}; {unfold_hc}mathocr_refute_at hc; trace \"mathocr:cex={label}\")")
        return stmt, "by\n  intro h\n  first\n  | " + "\n  | ".join(alts)

    # -- génération ----------------------------------------------------------------------

    def generate(self, *, probes: bool = True, fingerprints: bool = True) -> GeneratedLean:
        w = _Writer()
        for imp in self.ref.lean_imports:
            w.emit(f"import {imp}")
        w.emit("import MathOCRCheck.Auto")
        w.emit("")
        w.emit("/-! Fichier généré par MathOCR — gabarit de confiance.")
        w.emit("    Les fragments issus des agents (énoncés, définitions) sont filtrés par la politique")
        w.emit("    de sécurité ; toutes les preuves élémentaires viennent de `mathocr_core`. -/")
        w.emit("set_option autoImplicit false")
        w.emit("set_option maxHeartbeats 200000")
        w.emit("set_option linter.unusedVariables false")
        w.emit("set_option linter.unusedSimpArgs false")
        w.emit("set_option linter.unnecessarySimpa false")
        w.emit("set_option linter.style.longLine false")
        w.emit("set_option linter.unreachableTactic false")
        w.emit("set_option linter.unusedTactic false")
        for o in self.ref.lean_opens:
            w.emit(f"open {o}")
        w.emit("")
        w.block("reference", None, "Reference.enonce",
                "namespace Reference\n"
                f"/-- Énoncé de référence (validé par : {self.ref.validated_by or 'NON VALIDÉ'}). -/\n"
                f"def enonce : Prop := {self.ref.lean_statement}\n"
                "end Reference\n")
        w.emit(f"namespace {NS}")
        if self.defs:
            pass
        w.emit("")

        theorems: list[str] = []
        for step in self.st.steps:
            f = self._formal(step.id)
            if f is None or f.role in ("none", "hyp"):
                continue
            src = "; ".join(f"{r.line_id} « {r.excerpt} »" for r in step.source) or "(étape implicite)"
            doc = _doc(f"⟦{step.id}⟧ {step.kind} — {src}")
            if f.role == "def":
                w.block("def", step.id, f"{NS}.{f.def_name}",
                        f"/-- {doc} -/\ndef {f.def_name} {_binders_src(f.def_binders)} : Prop :=\n  {f.def_body}\n")
                continue
            binders = self._step_binders(step.id)
            bsrc = " ".join(f"({n} : {t})" for n, t in binders)
            if step.implicit and step.implicit_reason == "schema_recurrence":
                proof = self._induction_proof(step.id)
            elif self.st.pattern.kind == "recurrence_simple" and step.id == self.st.pattern.conclusion_step:
                # « P(0) et P(n) ⇒ P(n+1), alors … » : l'élève invoque explicitement le principe de
                # récurrence pour conclure. Seul le gabarit de récurrence (de confiance) est ajouté.
                proof = self._elementary_proof() + "\n  | " + self._induction_inline(step.id)
            else:
                proof = self._elementary_proof()
            w.block("step", step.id, f"{NS}.{step.id}",
                    f"/-- {doc} -/\ntheorem {step.id} {bsrc} :\n    {f.claim} := {proof}\n")
            theorems.append(f"{NS}.{step.id}")

            if f.agent_proof:
                body = "\n".join("  " + ln for ln in f.agent_proof.strip().splitlines())
                w.block("agent_proof", step.id, f"{NS}.{step.id}_agent",
                        f"/-- Preuve proposée par un agent pour {step.id} (niveau 2). -/\n"
                        f"theorem {step.id}_agent {bsrc} :\n    {f.claim} := by\n{body}\n")
            if f.agent_refutation:
                body = "\n".join("  " + ln for ln in f.agent_refutation.strip().splitlines())
                stmt = f"¬ (∀ {bsrc}, {f.claim})" if bsrc else f"¬ ({f.claim})"
                w.block("agent_refutation", step.id, f"{NS}.{step.id}_refutation_agent",
                        f"theorem {step.id}_refutation_agent : {stmt} := by\n{body}\n")
            if probes and f.uses and not step.implicit:
                vb = " ".join(f"({n} : {t})" for n, t in binders if not n.startswith("h_"))
                w.block("sans_dep", step.id, f"{NS}.{step.id}_sans_dependances",
                        f"theorem {step.id}_sans_dependances {vb} :\n    {f.claim} := {self._elementary_proof()}\n")
            if probes and not (step.implicit and step.implicit_reason == "schema_recurrence"):
                ref = self._refutation_proof(step.id)
                if ref:
                    stmt, prf = ref
                    w.block("refutation", step.id, f"{NS}.{step.id}_refutation",
                            f"theorem {step.id}_refutation : {stmt} := {prf}\n")

        # Assemblage : la conclusion de l'élève, obtenue en composant ses étapes.
        concl = self.st.pattern.conclusion_step
        cf = self._formal(concl) if concl else None
        if concl and cf and cf.claim:
            cstep = self.st.step(concl)
            vs = self._vars(cstep.scope)
            stmt = f"∀ {_binders_src(vs)}, {cf.claim}" if vs else cf.claim
            body = self.term(concl, cstep.scope)
            if vs:
                body = f"fun {' '.join(b.name for b in vs)} => {body}"
            w.block("assembly", concl, f"{NS}.assemblage",
                    f"/-- Assemblage des étapes de la copie (composition des théorèmes ci-dessus). -/\n"
                    f"theorem assemblage : {stmt} :=\n  {body}\n")
            unfold = f"(simp only [{', '.join(self.defs)}] at hc ⊢; first | exact hc | mathocr_core | (intros; mathocr_core))" if self.defs else "mathocr_core"
            w.block("accord", None, f"{NS}.accord_enonce",
                    "/-- La conclusion de la copie entraîne l'énoncé de référence. -/\n"
                    "theorem accord_enonce : Reference.enonce := by\n"
                    "  unfold Reference.enonce\n"
                    "  have hc := assemblage\n"
                    "  first\n  | exact hc\n  | (simpa using hc)\n"
                    f"  | {unfold}\n  | (intros; mathocr_core)\n")
            theorems += [f"{NS}.assemblage", f"{NS}.accord_enonce"]
        w.emit(f"end {NS}")
        w.emit("")

        # Empreintes numériques (évaluation des deux membres aux points d'échantillonnage).
        if fingerprints:
            for f in self.fm.steps:
                if not f.sides or f.sides.value_type not in EVAL_TYPES:
                    continue
                vs = f.sides.variables
                if any(b.type not in SAMPLE_VALUES for b in vs):
                    continue
                pts = list(itertools.product(*[SAMPLE_VALUES[b.type] for b in vs]))[:MAX_POINTS] if vs else [()]
                for pt in pts:
                    if f.sides.value_type in ("ℕ", "Nat") and any(v.startswith("-") for v in pt):
                        continue
                    for side, expr in (("lhs", f.sides.lhs_lean), ("rhs", f.sides.rhs_lean)):
                        if vs:
                            fun = f"(fun {_binders_src(vs)} => (({expr}) : {f.sides.value_type}))"
                            call = f"{fun} " + " ".join(f"({v} : {b.type})" for v, b in zip(pt, vs))
                        else:
                            call = f"(({expr}) : {f.sides.value_type})"
                        w.block("eval", f.step_id, None, f"open {NS} in #eval {call}",
                                side=side, point=dict(zip([b.name for b in vs], pt)))

        # Contrôle des axiomes de TOUS les théorèmes générés (étapes, preuves et réfutations d'agents,
        # sondes) : aucun résultat ne compte s'il dépend de sorryAx ou d'un axiome hors liste.
        all_decls = [s.decl for s in w.segments if s.decl and s.kind not in ("def", "reference", "eval")]
        for t in dict.fromkeys(theorems + all_decls):
            w.block("axioms", None, t, f"#print axioms {t}")
        return GeneratedLean("\n".join(w.lines) + "\n", w.segments, self.violations, theorems)
