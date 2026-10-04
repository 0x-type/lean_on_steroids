"""Théorèmes cités par l'élève : catalogue, repérage dans la copie, contrôle de la preuve Lean.

Un enseignant accepte « d'après le TVI, il existe c… » sans redémonstration. Le système fait de même,
à trois conditions vérifiées par du code :
1. le théorème appartient au catalogue (src/leanonsteroids/data/theoremes.json), dont chaque lemme Mathlib
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


_SIGLE = re.compile(r"(?<![A-Za-z])([A-Z](?:\.?[A-Z]){2,})\.?(?![A-Za-z])")


def _sigles_proches(texte: str, cat: Catalogue) -> list[str]:
    """Sigle écrit avec une lettre de travers (« TVA » pour TVI) : même longueur, même première lettre, une
    seule lettre différente, et pas lui-même le sigle d'un autre théorème. La citation n'est acceptée que si
    Lean montre que l'étape emploie bien ce théorème (juger_usages)."""
    actifs = cat.actifs()
    sigles = {a.replace(".", ""): k for k, th in actifs.items() for a in th.alias
              if a.replace(".", "").isupper() and a.replace(".", "").isalpha() and len(a.replace(".", "")) >= 3}
    out = []
    for m in _SIGLE.finditer(texte):
        w = m.group(1).replace(".", "")
        if w in sigles:
            continue
        for sig, k in sigles.items():
            if len(sig) == len(w) and sig[0] == w[0] and sum(a != b for a, b in zip(sig, w)) == 1 and k not in out:
                out.append(k)
    return out


def reperer(texte: str, cat: Catalogue | None = None, approx: bool = False) -> list[str]:
    """Clés des théorèmes (actifs) dont un nom figure dans le texte, mot pour mot (aux accents près) ;
    avec `approx`, aussi les sigles à une lettre près (« TVA » écrit pour « TVI »)."""
    cat = cat or charger()
    t = _plat(texte)
    out = [k for k, th in cat.actifs().items() if any(_plat(a) in t for a in th.alias)]
    if approx:
        out += [k for k in _sigles_proches(texte, cat) if k not in out]
    return out


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
        cles = set(reperer(texte, cat, approx=True))
        for c in getattr(s, "citations", []) or []:
            if c.theoreme in cat.actifs() and _plat(c.extrait).strip() and _plat(c.extrait) in _plat(texte) \
                    and c.theoreme in reperer(c.extrait, cat, approx=True):
                cles.add(c.theoreme)
        if cles:
            out[s.id] = sorted(cles)
    return out


def cles_kit(usages: list[tuple], kit_theoremes: dict[str, str], cites_copie) -> list[str]:
    """Théorèmes cités ailleurs dans la copie et employés ici à travers un lemme du kit."""
    return sorted({kit_theoremes[u[0]] for u in usages
                   if u[1] == "Kit" and kit_theoremes.get(u[0]) in set(cites_copie)})


def juger_usages(cles: list[str], usages: list[tuple], cat: Catalogue | None = None,
                 permis_exercice: set[str] | frozenset = frozenset(),
                 kit_theoremes: dict[str, str] | None = None) -> tuple[bool, str]:
    """La preuve Lean d'une étape s'appuie-t-elle exactement sur le(s) théorème(s) cité(s) ?

    `usages` : (théorème, module[, règle de simplification]) utilisés directement par la preuve. Une règle de
    simplification de la bibliothèque (@[simp]) est un fait de base, sauf si c'est un théorème du catalogue.
    Retourne (accepté, explication)."""
    cat = cat or charger()
    kit_theoremes = kit_theoremes or {}
    # Un lemme du kit qui est un grand théorème (le TVI mis en forme pour l'exercice) compte comme ce théorème.
    kit_hors = sorted({f"{u[0]} ({cat.theoremes[kit_theoremes[u[0]]].nom if kit_theoremes[u[0]] in cat.theoremes else kit_theoremes[u[0]]})"
                       for u in usages if u[1] == "Kit" and u[0] in kit_theoremes and kit_theoremes[u[0]] not in cles})
    if kit_hors:
        return False, f"la preuve utilise {', '.join(kit_hors)}, que la copie ne cite pas"
    kit_utilises = sorted({u[0] for u in usages if u[1] == "Kit" and kit_theoremes.get(u[0]) in cles})
    cites = [cat.theoremes[k] for k in cles if k in cat.theoremes]
    permis = {lem for t in cites for lem in t.lemmes + t.compagnons} | cat.elementaires_avances | set(permis_exercice)
    nommes = cat.lemmes_nommes()
    gros = sorted({u[0] for u in usages
                   if (u[0] in nommes or (cat.avance(u[1]) and not (len(u) > 2 and u[2])))
                   and u[0] not in cat.elementaires_avances and u[1] != "Kit" and u[0] not in permis_exercice})
    hors = [n for n in gros if n not in permis]
    if hors:
        return False, f"la preuve utilise aussi {', '.join(hors)}, que la copie ne cite pas"
    if cites:
        utilises = [n for n in gros if any(n in t.lemmes for t in cites)] + kit_utilises
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


# Assemblage par agent : de la logique (cas, absurde, témoins, ∀/∃) et de l'arithmétique linéaire pour les
# conditions annexes, rien qui puisse ajouter un raisonnement mathématique absent de la copie.
TACTIQUES_INTERDITES_ASSEMBLAGE = {
    "simp", "simp_all", "simpa", "nlinarith", "norm_num", "ring", "ring_nf", "field_simp", "aesop", "decide",
    "polyrith", "grind", "linear_combination", "bound", "gcongr", "interval_cases", "mathocr_core", "mathocr_kit",
    "exact?", "apply?", "hint", "norm_cast", "push_cast", "nlinarith!", "positivity", "abel", "group", "noncomm_ring"}
MODULES_LOGIQUES = ("Init", "Std", "Batteries", "Mathlib.Logic", "Mathlib.Order", "Mathlib.Tactic",
                    "Mathlib.Algebra.Order")


def juger_assemblage(texte: str, usages: list[tuple], kit_theoremes: dict[str, str] | None = None,
                     cites_copie=()) -> tuple[bool, str, list[str]]:
    """Un assemblage proposé par un agent n'ajoute-t-il rien aux étapes de l'élève ?

    Retourne (accepté, explication, étapes de la copie utilisées)."""
    kit_hors = sorted({u[0] for u in usages if u[1] == "Kit" and (kit_theoremes or {}).get(u[0])
                       and kit_theoremes[u[0]] not in set(cites_copie)})
    mots = set(re.findall(r"[A-Za-z_][\w!?']*", texte))
    interdits = sorted(mots & TACTIQUES_INTERDITES_ASSEMBLAGE)
    if interdits:
        return False, f"l'assemblage emploie {', '.join(interdits)}, qui pourrait ajouter un calcul absent de la copie", []
    etapes = sorted({u[0] for u in usages if u[1] == "Copie"})
    if kit_hors:
        return False, f"l'assemblage utilise {', '.join(kit_hors)}, un théorème que la copie ne cite pas", etapes
    # Seuls les lemmes ÉCRITS dans l'assemblage doivent être de la logique ; ceux qu'emploie linarith ou omega
    # en interne relèvent de l'arithmétique linéaire, qu'on autorise pour les conditions annexes.
    ecrits = {m.split(".")[-1] for m in mots}
    autres = sorted({u[0] for u in usages if u[1] not in ("Copie", "Kit") and u[0].split(".")[-1] in ecrits
                     and not any(u[1] == m or u[1].startswith(m + ".") for m in MODULES_LOGIQUES)})
    if autres:
        return False, f"l'assemblage utilise {', '.join(autres)}, qui n'est pas de la logique pure", etapes
    if not etapes:
        return False, "l'assemblage n'utilise aucune étape de la copie", etapes
    return True, "assemblage logique des étapes de la copie (" + ", ".join(e.split(".")[-1] for e in etapes) + ")", etapes
