"""Schémas de données de bout en bout.

Chaque étape du pipeline produit un objet de ce module, sérialisé en JSON dans
le répertoire de l'exécution. Le principe directeur est la *traçabilité* :
toute étape Lean pointe vers une étape de preuve, qui pointe vers des lignes
de la transcription, qui pointent vers une zone de la photo.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Entrées
# ---------------------------------------------------------------------------


class ReferenceStatement(BaseModel):
    """Énoncé de l'exercice et sa formalisation de référence.

    La formalisation de l'énoncé est faite une fois par exercice et doit être
    validée par un enseignant : c'est elle qui fixe *ce qu'il faut démontrer*.
    Tant que `validated_by` est vide, l'issue « raisonnement vérifié » est
    interdite.
    """

    exercise_id: str
    statement_latex: str
    notes_latex: str = ""
    lean_imports: list[str] = Field(default_factory=lambda: ["MathOCRCheck.Prelude"])
    lean_opens: list[str] = Field(default_factory=lambda: ["Finset"])
    # Proposition Lean (terme de type Prop) qui formalise l'énoncé.
    lean_statement: str
    validated_by: str | None = None
    # Contexte de l'exercice (« Soit f continue telle que … ») : objets et hypothèses fixés par l'énoncé,
    # écrits une fois par exercice et validés avec lui. Chaque étape peut s'en servir sans les redéclarer ;
    # l'énoncé de référence doit être de la forme ∀ objets, hypothèses → but (même ordre).
    contexte: "Contexte | None" = None


class ObjetContexte(BaseModel):
    nom: str  # identifiant Lean, ex. « f »
    type: str  # type Lean, ex. « ℝ → ℝ »
    latex: str = ""  # tel qu'écrit dans l'énoncé, ex. « f »


class HypotheseContexte(BaseModel):
    nom: str  # identifiant Lean commençant par h_, ex. « h_cont »
    lean: str  # proposition Lean, ex. « Continuous f »
    latex: str = ""  # ex. « f est continue sur ℝ »


class Contexte(BaseModel):
    objets: list[ObjetContexte] = Field(default_factory=list)
    hypotheses: list[HypotheseContexte] = Field(default_factory=list)

    def binders(self) -> list[tuple[str, str]]:
        return [(o.nom, o.type) for o in self.objets] + [(h.nom, h.lean) for h in self.hypotheses]

    def decrire(self) -> str:
        """Pour les consignes des agents : « (f : ℝ → ℝ) (a : ℝ) (h_cont : Continuous f) … »."""
        return " ".join(f"({n} : {t})" for n, t in self.binders())


class PageImage(BaseModel):
    page: int
    path: str
    width: int
    height: int
    sha256: str


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

BBox = tuple[int, int, int, int]  # x0, y0, x1, y1 en pixels de l'image d'origine


class LineStatus(str, Enum):
    normal = "normal"
    crossed_out = "barré"
    illegible = "illisible"
    decoration = "hors_sujet"  # rature isolée, gribouillis, numéro d'exercice…


class Reading(BaseModel):
    text: str
    support: list[str] = Field(default_factory=list)  # moteurs qui proposent cette lecture
    score: float = 0.0


class Uncertainty(BaseModel):
    id: str
    line_id: str
    span: str  # le fragment concerné, tel que lu dans la version retenue
    readings: list[Reading]  # lectures possibles, la retenue en premier
    chosen: str
    reason: str
    # Vrai si le choix repose sur le sens mathématique (l'énoncé, la suite du
    # calcul) plutôt que sur la forme du tracé : c'est exactement le cas où un
    # OCR « corrige » silencieusement l'élève.
    context_dependent: bool = False
    # Vrai si la lecture alternative s'applique à toutes les occurrences du
    # fragment dans la ligne (ex. lettre d'indice k/h tracée de la même façon).
    replace_all: bool = False
    # Rempli par l'analyse de sensibilité (étape fidélité) :
    blocking: bool | None = None
    resolution: str | None = None


class TranscribedLine(BaseModel):
    id: str  # "p1.L07"
    page: int
    bbox: BBox
    text: str  # texte français + maths en $…$, fidèle au tracé
    status: LineStatus = LineStatus.normal
    confidence: float = 1.0
    engine_readings: dict[str, str] = Field(default_factory=dict)


class EngineRun(BaseModel):
    engine: str
    model: str
    mode: Literal["aveugle", "avec_contexte", "arbitrage", "fixture", "cascade_lignes", "audit"]
    ok: bool
    error: str | None = None
    seconds: float | None = None
    note: str | None = None


class Transcription(BaseModel):
    pages: list[PageImage]
    lines: list[TranscribedLine]
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    engines: list[EngineRun] = Field(default_factory=list)
    provenance: str = ""  # comment cette transcription a été obtenue

    def line(self, line_id: str) -> TranscribedLine:
        for ln in self.lines:
            if ln.id == line_id:
                return ln
        raise KeyError(line_id)


# ---------------------------------------------------------------------------
# Structure de la preuve (graphe d'étapes)
# ---------------------------------------------------------------------------

StepKind = Literal[
    "definition",  # « on note P(n) la propriété … »
    "introduction",  # « soit n ∈ ℕ fixé »
    "hypothese",  # « supposons P(n) »
    "annonce",  # « montrons que P(n+1) » : pas une affirmation
    "affirmation",  # une égalité, une inégalité, « P(0) est vraie »…
    "calcul",  # un maillon d'une chaîne d'égalités
    "conclusion",
    "commentaire",
]


class SourceRef(BaseModel):
    line_id: str
    excerpt: str  # sous-chaîne exacte de la ligne transcrite


class Citation(BaseModel):
    """Théorème que l'élève invoque pour justifier l'étape (clé du catalogue + mots exacts de la copie)."""

    theoreme: str
    extrait: str


class ProofStep(BaseModel):
    id: str
    kind: StepKind
    scope: str = "global"
    statement: str  # l'affirmation telle qu'écrite (LaTeX), sans correction
    justification: str | None = None  # justification écrite par l'élève
    connective: str | None = None  # « donc », « alors », « = », « c-à-d »…
    depends_on: list[str] = Field(default_factory=list)
    # Étape non écrite mais imposée par la forme de la rédaction
    # (ex. transitivité d'une chaîne d'égalités). Liste fermée : voir
    # IMPLICIT_ALLOWED.
    implicit: bool = False
    implicit_reason: Literal["", "chaine_egalites", "depliage_definition", "schema_recurrence"] = ""
    source: list[SourceRef] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)  # théorèmes cités par l'élève pour cette étape


class Scope(BaseModel):
    id: str
    parent: str | None = "global"
    variables: list[str] = Field(default_factory=list)  # ex. ["n"]
    assumptions: list[str] = Field(default_factory=list)  # ids d'étapes « hypothese »


class ProofPattern(BaseModel):
    kind: Literal["direct", "recurrence_simple", "aucun"]
    base_step: str | None = None
    heredity_scope: str | None = None
    heredity_step: str | None = None
    conclusion_step: str | None = None
    variable: str | None = None


class Observation(BaseModel):
    step_ids: list[str] = Field(default_factory=list)
    severity: Literal["info", "redaction", "lacune", "erreur"]
    text: str


class ProofStructure(BaseModel):
    scopes: list[Scope]
    steps: list[ProofStep]
    pattern: ProofPattern
    observations: list[Observation] = Field(default_factory=list)
    unmapped_lines: list[str] = Field(default_factory=list)  # lignes non rattachées (doit être justifié)

    def step(self, sid: str) -> ProofStep:
        for s in self.steps:
            if s.id == sid:
                return s
        raise KeyError(sid)


# ---------------------------------------------------------------------------
# Formalisation
# ---------------------------------------------------------------------------


class Binder(BaseModel):
    name: str
    type: str  # type Lean, ex. "ℕ"


class Sides(BaseModel):
    """Pour une (in)égalité : membres en Lean ET en LaTeX (copiés de la copie).

    Sert à l'empreinte numérique : on évalue les deux versions aux mêmes
    points et on compare.
    """

    relation: Literal["=", "<", "≤", ">", "≥", "≠"]
    lhs_lean: str
    rhs_lean: str
    lhs_latex: str
    rhs_latex: str
    value_type: str = "ℕ"
    # Variables libres des deux membres (ex. n) et leur type Lean.
    variables: list[Binder] = Field(default_factory=list)


class StepFormal(BaseModel):
    step_id: str
    role: Literal["def", "prop", "hyp", "none"]
    # role=def : définition introduite par l'élève (ex. P).
    def_name: str | None = None
    def_binders: list[Binder] = Field(default_factory=list)
    def_body: str | None = None
    # role=prop|hyp : proposition Lean, dans le contexte de la portée.
    claim: str | None = None
    uses: list[str] = Field(default_factory=list)  # étapes utilisées comme hypothèses
    sides: Sides | None = None
    # Lecture de l'argument pour « P(t) est vraie » : on vérifie que t apparaît dans la copie.
    predicate_app: tuple[str, str] | None = None  # (nom, argument en LaTeX)
    not_formalized_reason: str | None = None
    # Preuve proposée par un agent si l'automatisation élémentaire échoue
    # (niveau 2). Elle ne peut pas changer l'énoncé de l'étape.
    agent_proof: str | None = None
    agent_refutation: str | None = None
    # Théorèmes cités par l'élève et retrouvés dans la copie (clés du catalogue) : la preuve de niveau 2
    # peut s'appuyer dessus, et seulement sur eux.
    cites: list[str] = Field(default_factory=list)
    # Qui a produit cette formalisation : code (traduction déterministe), agent,
    # mémoire (étape déjà validée sur une autre copie), fixture.
    origin: str = ""


class ScopeFormal(BaseModel):
    scope_id: str
    binders: list[Binder]


class Formalization(BaseModel):
    scopes: list[ScopeFormal]
    steps: list[StepFormal]
    provenance: str = ""

    def of(self, sid: str) -> StepFormal:
        for s in self.steps:
            if s.step_id == sid:
                return s
        raise KeyError(sid)


# ---------------------------------------------------------------------------
# Vérification Lean
# ---------------------------------------------------------------------------


class LeanMessage(BaseModel):
    severity: Literal["error", "warning", "information"]
    line: int
    col: int
    text: str
    step_id: str | None = None


class StepCheck(BaseModel):
    step_id: str
    decl: str
    status: Literal[
        "verifie_elementaire",  # prouvé par l'automatisation élémentaire à partir des seules dépendances
        "verifie_theoreme",  # prouvé avec le théorème cité par l'élève (et seulement lui) : justifié
        "verifie_agent",  # prouvé seulement avec une preuve fournie par un agent : saut logique
        "refute",  # la négation est démontrée dans Lean (contre-exemple certifié)
        "non_verifie",  # ni prouvé ni réfuté
        "hypothese",  # hypothèse de la portée, pas à prouver
        "definition",
        "non_formalise",
        "erreur_formalisation",  # l'énoncé Lean ne compile pas
    ]
    closed_by: str | None = None
    counterexample: str | None = None
    uses: list[str] = Field(default_factory=list)  # théorèmes non élémentaires utilisés par la preuve de niveau 2
    independent_of_deps: bool | None = None  # vrai si l'étape se prouve sans ses dépendances déclarées
    messages: list[LeanMessage] = Field(default_factory=list)
    lean_snippet: str = ""
    lean_lines: tuple[int, int] | None = None


class PolicyViolation(BaseModel):
    where: str
    token: str
    detail: str


class LeanRun(BaseModel):
    ok: bool
    lean_version: str = ""
    returncode: int | None = None
    timed_out: bool = False
    seconds: float = 0.0
    sandbox: str = ""
    messages: list[LeanMessage] = Field(default_factory=list)
    stderr_tail: str = ""


class LeanReport(BaseModel):
    file: str
    policy_violations: list[PolicyViolation] = Field(default_factory=list)
    run: LeanRun | None = None
    steps: list[StepCheck] = Field(default_factory=list)
    assembly_ok: bool = False
    statement_match: bool = False  # la conclusion de l'élève implique l'énoncé de référence
    axioms: dict[str, list[str]] = Field(default_factory=dict)
    axioms_ok: bool = False


# ---------------------------------------------------------------------------
# Fidélité, verdict, retour
# ---------------------------------------------------------------------------


class FidelityCheck(BaseModel):
    step_id: str
    kind: Literal[
        "ancrage",  # l'extrait cité existe dans la ligne transcrite
        "empreinte_numerique",  # Lean et LaTeX de la copie s'évaluent pareil
        "application_predicat",  # « P(t) » : même prédicat, même argument
        "retrotraduction",  # un agent relit le Lean sans voir la copie
        "couverture",  # toutes les lignes utiles sont rattachées à une étape
        "structure",  # pas d'étape Lean sans étape écrite
        "sensibilite_lecture",  # une lecture alternative changerait-elle la formalisation ?
    ]
    ok: bool | None  # None = non évaluable
    detail: str


class Verdict(str, Enum):
    verified = "raisonnement vérifié"
    error = "erreur mathématique établie"
    review = "examen nécessaire"


class VerdictReport(BaseModel):
    verdict: Verdict
    reasons: list[str]
    blocking_issues: list[str] = Field(default_factory=list)
    remarks: list[str] = Field(default_factory=list)


class Feedback(BaseModel):
    summary: str
    points_forts: list[str] = Field(default_factory=list)
    points_a_corriger: list[str] = Field(default_factory=list)
    conseils_redaction: list[str] = Field(default_factory=list)
    # Mode tolérant : justifications absentes mais étapes vraies (démontrées par Lean), signalées à l'élève.
    pour_une_redaction_parfaite: list[str] = Field(default_factory=list)
    note_pour_correcteur: str = ""
    generated_by: str = "gabarit déterministe"


class RunResult(BaseModel):
    exercise: ReferenceStatement
    transcription: Transcription
    structure: ProofStructure
    formalization: Formalization
    lean: LeanReport
    fidelity: list[FidelityCheck]
    verdict: VerdictReport
    feedback: Feedback
    costs: "CostReport | None" = None


# ---------------------------------------------------------------------------
# Coûts
# ---------------------------------------------------------------------------


class UsageEntry(BaseModel):
    stage: str
    engine: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    images: int = 0
    cached: bool = False  # réponse servie par le cache : gratuite
    cost_usd: float | None = None  # None = prix inconnu


class CostReport(BaseModel):
    entries: list[UsageEntry] = Field(default_factory=list)
    total_usd: float = 0.0
    complete: bool = True  # faux si au moins un appel n'a pas de prix connu
    llm_calls: int = 0
    cached_calls: int = 0
    lean_seconds: float = 0.0


RunResult.model_rebuild()


ReferenceStatement.model_rebuild()
