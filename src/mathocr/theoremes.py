"""Théorèmes cités par l'élève : catalogue, repérage dans la copie, contrôle de la preuve Lean.

Un enseignant accepte « d'après le TVI, il existe c… » sans redémonstration. Le système fait de même,
à trois conditions vérifiées par du code :
1. le théorème appartient au catalogue (src/mathocr/data/theoremes.json), dont chaque lemme Mathlib
   a été vérifié par Lean (scripts/verifier_catalogue.py) ;
2. l'élève a écrit son nom : la citation est retrouvée dans les lignes de l'étape (ou la ligne juste
   avant), jamais seulement suggérée par un agent ;
3. Lean démontre l'étape à partir des seules dépendances déclarées, et les théorèmes non élémentaires
   effectivement utilisés par la preuve sont le lemme cité et ses compagnons, rien d'autre.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CATALOGUE = Path(__file__).parent / "data" / "theoremes.json"

# Une étape que Lean démontre (niveau 2) sans aucun théorème non élémentaire est acceptée comme les étapes
# démontrées par l'automatisation de base : un correcteur n'exige pas la justification d'un calcul. Elle
# est signalée dans le rapport. Mettre à False pour revenir à « toute preuve d'agent = saut logique ».
ACCEPTER_PREUVE_ELEMENTAIRE = True


@dataclass
class Theoreme:
    cle: str
    nom: str
    alias: list[str]
    lemmes: list[str]
    compagnons: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    verifie: bool = False


@dataclass
class Catalogue:
    theoremes: dict[str, Theoreme]
    modules_avances: list[str]
    elementaires_avances: set[str]

    def actifs(self) -> dict[str, Theoreme]:
        """Entrées dont les lemmes ont été vérifiés par Lean."""
        return {k: t for k, t in self.theoremes.items() if t.verifie}

    def lemmes_nommes(self) -> set[str]:
        """Tous les grands théorèmes du catalogue : les utiliser sans les citer est un saut logique."""
        return {lem for t in self.theoremes.values() for lem in t.lemmes}

    def avance(self, module: str) -> bool:
        return any(module == p or module.startswith(p + ".") for p in self.modules_avances)


@lru_cache(maxsize=4)
def charger(path: str | None = None) -> Catalogue:
    data = json.loads(Path(path or CATALOGUE).read_text(encoding="utf-8"))
    th = {e["cle"]: Theoreme(**{k: v for k, v in e.items() if k in Theoreme.__dataclass_fields__})
          for e in data["theoremes"]}
    return Catalogue(th, data.get("modules_avances", []), set(data.get("elementaires_avances", [])))


def _plat(s: str) -> str:
    """Minuscules, sans accents ni ponctuation : « Théorème des Valeurs Intermédiaires » ~ « theoreme des valeurs intermediaires »."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\\[a-z]+\{([^}]*)\}", r"\1", s)  # \text{TVI} -> TVI
    mots = re.sub(r"[^a-z0-9]+", " ", s).split()
    # Pluriels et accords ignorés (« facteur premiers » ~ « facteurs premiers »), pas les sigles (TVA ≠ TVI).
    mots = [m[:-1] if len(m) > 3 and m.endswith("s") else m for m in mots]
    return " " + " ".join(mots) + " "


def reperer(texte: str, cat: Catalogue | None = None) -> list[str]:
    """Clés des théorèmes (actifs) dont un nom figure dans le texte, mot pour mot (aux accents près)."""
    cat = cat or charger()
    t = _plat(texte)
    return [k for k, th in cat.actifs().items() if any(_plat(a) in t for a in th.alias)]


def citations_ancrees(st, tr, cat: Catalogue | None = None) -> dict[str, list[str]]:
    """{étape: [clés]} des théorèmes cités, retrouvés dans le texte écrit par l'élève.

    On cherche dans les lignes de l'étape et dans la ligne qui précède la première d'entre elles
    (« … on applique le TVI : / il existe c … »). Une citation proposée par l'agent de structure n'est
    retenue que si son extrait figure lui aussi dans ces lignes et désigne le même théorème."""
    cat = cat or charger()
    order = [ln.id for ln in tr.lines]
    out: dict[str, list[str]] = {}
    for s in st.steps:
        ids = [r.line_id for r in s.source if r.line_id in order]
        if not ids:
            continue
        first = min(order.index(i) for i in ids)
        if first > 0:
            ids.append(order[first - 1])
        texte = " ".join(tr.line(i).text for i in ids)
        cles = set(reperer(texte, cat))
        for c in getattr(s, "citations", []) or []:
            if c.theoreme in cat.actifs() and _plat(c.extrait).strip() and _plat(c.extrait) in _plat(texte) \
                    and c.theoreme in reperer(c.extrait, cat):
                cles.add(c.theoreme)
        if cles:
            out[s.id] = sorted(cles)
    return out


def juger_usages(cles: list[str], usages: list[tuple[str, str]], cat: Catalogue | None = None) -> tuple[bool, str]:
    """La preuve Lean d'une étape s'appuie-t-elle exactement sur le(s) théorème(s) cité(s) ?

    `usages` : (théorème, module[, règle de simplification]) utilisés directement par la preuve. Une règle de
    simplification de la bibliothèque (@[simp]) est un fait de base, sauf si c'est un théorème du catalogue.
    Retourne (accepté, explication)."""
    cat = cat or charger()
    cites = [cat.theoremes[k] for k in cles if k in cat.theoremes]
    permis = {lem for t in cites for lem in t.lemmes + t.compagnons} | cat.elementaires_avances
    nommes = cat.lemmes_nommes()
    gros = sorted({u[0] for u in usages
                   if (u[0] in nommes or (cat.avance(u[1]) and not (len(u) > 2 and u[2])))
                   and u[0] not in cat.elementaires_avances})
    hors = [n for n in gros if n not in permis]
    if hors:
        return False, f"la preuve utilise aussi {', '.join(hors)}, que la copie ne cite pas"
    if cites:
        utilises = [n for n in gros if any(n in t.lemmes for t in cites)]
        if not utilises:
            return False, "la preuve n'utilise pas le théorème cité"
        return True, f"{' et '.join(t.nom for t in cites)} (Mathlib : {', '.join(utilises)})"
    return (not gros), ("preuve élémentaire" if not gros else "théorème non cité")


def imports_pour(cles: list[str], cat: Catalogue | None = None) -> list[str]:
    cat = cat or charger()
    out: list[str] = []
    for k in cles:
        for imp in cat.theoremes[k].imports if k in cat.theoremes else []:
            if imp not in out:
                out.append(imp)
    return out
