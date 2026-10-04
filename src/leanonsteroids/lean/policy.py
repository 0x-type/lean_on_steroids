"""Politique de sécurité pour le code Lean produit par des agents.

Les agents ne fournissent jamais un fichier Lean complet : ils fournissent des
*fragments* (un terme de type Prop, le corps d'une définition, éventuellement
une preuve tactique) que le générateur insère dans un gabarit de confiance.
Chaque fragment passe par ce filtre avant insertion. Après compilation, on
contrôle en plus les axiomes effectivement utilisés (`#print axioms`).

Défense en profondeur :
  1. lexique : mots-clés de commande et échappatoires interdits dans les fragments ;
  2. forme : un fragment « terme » tient sur une ligne, sans commentaire ;
  3. après coup : `sorryAx`, `Lean.ofReduceBool` ou tout axiome hors de
     {propext, Classical.choice, Quot.sound} invalide le résultat.
"""

from __future__ import annotations

import re

from ..schemas import PolicyViolation

ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

# Échappatoires et moyens de sortir du gabarit.
FORBIDDEN_WORDS = {
    "sorry": "preuve laissée en suspens",
    "admit": "preuve laissée en suspens",
    "axiom": "ajout d'axiome",
    "axioms": "ajout d'axiome",
    "opaque": "déclaration opaque (peut cacher un axiome)",
    "unsafe": "code non sûr",
    "partial": "fonction partielle",
    "implemented_by": "substitution d'implémentation",
    "extern": "code externe",
    "native_decide": "utilise Lean.ofReduceBool (confiance dans le compilateur)",
    "ofReduceBool": "axiome de réduction native",
    "ofReduceNat": "axiome de réduction native",
    "reduceBool": "axiome de réduction native",
    "trustCompiler": "axiome de confiance dans le compilateur",
    "sorryAx": "preuve laissée en suspens",
    "debug": "options de débogage",
    "skipKernelTC": "désactive le noyau",
    "set_option": "modification d'options",
    "macro": "métaprogrammation",
    "macro_rules": "métaprogrammation",
    "syntax": "métaprogrammation",
    "elab": "métaprogrammation",
    "elab_rules": "métaprogrammation",
    "notation": "redéfinition de notation",
    "infix": "redéfinition de notation",
    "infixl": "redéfinition de notation",
    "infixr": "redéfinition de notation",
    "prefix": "redéfinition de notation",
    "postfix": "redéfinition de notation",
    "instance": "ajout d'instance",
    "attribute": "modification d'attributs",
    "import": "import supplémentaire",
    "namespace": "changement d'espace de noms",
    "section": "changement de section",
    "end": "fermeture de portée",
    "variable": "variables de section",
    "universe": "univers",
    "open": "ouverture d'espace de noms",
    "export": "export",
    "mutual": "déclarations mutuelles",
    "theorem": "déclaration imbriquée",
    "lemma": "déclaration imbriquée",
    "def": "déclaration imbriquée",
    "abbrev": "déclaration imbriquée",
    "example": "déclaration imbriquée",
    "structure": "déclaration imbriquée",
    "class": "déclaration imbriquée",
    "inductive": "déclaration imbriquée",
    "private": "déclaration imbriquée",
    "protected": "déclaration imbriquée",
    "noncomputable": "déclaration imbriquée",
    "run_tac": "exécution de méta-code",
    "run_cmd": "exécution de méta-code",
    "run_elab": "exécution de méta-code",
    "IO": "entrées/sorties",
    "unsafeCast": "conversion non sûre",
    "unsafeIO": "entrées/sorties non sûres",
    "decreasing_by": "preuve de terminaison ad hoc",
    "termination_by": "preuve de terminaison ad hoc",
    "Lean": "accès au méta-niveau (Lean.*)",
    "Elab": "accès au méta-niveau",
    "Meta": "accès au méta-niveau",
    "Tactic": "accès au méta-niveau",
}

FORBIDDEN_PATTERNS = [
    (re.compile(r"#\w+"), "commande # (#eval, #exit, #print…) interdite dans un fragment"),
    (re.compile(r"@\["), "attribut interdit dans un fragment"),
    (re.compile(r"--|/-|-/"), "commentaire interdit dans un fragment (pourrait masquer du code)"),
    (re.compile(r"\bstop\b"), "commande stop"),
    (re.compile(r"[`]\("), "quotation de syntaxe"),
    (re.compile(r"\$\{|%\["), "antiquotation"),
]

_IDENT = re.compile(r"[A-Za-z_À-ɏ][A-Za-z0-9_'!?À-ɏ]*")


def _identifiers(fragment: str) -> list[str]:
    # Découpe aussi les noms qualifiés : Lean.ofReduceBool -> Lean, ofReduceBool
    return _IDENT.findall(fragment)


def check_fragment(fragment: str, where: str, *, multiline: bool = False) -> list[PolicyViolation]:
    """Vérifie un fragment fourni par un agent. Retourne la liste des violations."""
    out: list[PolicyViolation] = []
    if not multiline and ("\n" in fragment or "\r" in fragment):
        out.append(PolicyViolation(where=where, token="\\n", detail="un terme doit tenir sur une ligne"))
    # Un terme tient sur une ligne ; une preuve tactique d'agent peut être longue (Lean borne de toute
    # façon le temps et la mémoire), mais pas démesurée.
    if len(fragment) > (16000 if multiline else 4000):
        out.append(PolicyViolation(where=where, token="<longueur>", detail="fragment trop long"))
    for ident in _identifiers(fragment):
        if ident in FORBIDDEN_WORDS:
            out.append(PolicyViolation(where=where, token=ident, detail=FORBIDDEN_WORDS[ident]))
    for pat, detail in FORBIDDEN_PATTERNS:
        m = pat.search(fragment)
        if m:
            out.append(PolicyViolation(where=where, token=m.group(0), detail=detail))
    # Un fragment ne doit pas pouvoir fermer le bloc dans lequel il est inséré.
    depth = 0
    for ch in fragment:
        if ch in "([{⟨":
            depth += 1
        elif ch in ")]}⟩":
            depth -= 1
            if depth < 0:
                out.append(PolicyViolation(where=where, token=ch, detail="parenthèse fermante orpheline"))
                break
    if depth > 0:
        out.append(PolicyViolation(where=where, token="(", detail="parenthèse non fermée"))
    return out


def check_identifier(name: str, where: str) -> list[PolicyViolation]:
    # Lettres grecques admises (α, ε, δ… courantes en mathématiques), sauf λ, mot-clé de Lean.
    if not re.fullmatch(r"[A-Za-zα-κμ-ω][A-Za-z0-9_'α-κμ-ω₀-₉]*", name) or name in FORBIDDEN_WORDS:
        return [PolicyViolation(where=where, token=name, detail="identifiant invalide")]
    return []


def check_axioms(axioms: dict[str, list[str]]) -> list[str]:
    """Retourne la liste des problèmes d'axiomes (vide si tout va bien)."""
    problems = []
    for decl, axs in axioms.items():
        for ax in axs:
            if ax not in ALLOWED_AXIOMS:
                problems.append(f"{decl} dépend de l'axiome non autorisé {ax}")
    return problems
