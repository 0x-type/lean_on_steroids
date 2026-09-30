"""Génère les variantes de test à partir de la transcription de référence.

Chaque variante modifie la copie *au niveau de la transcription* (pas de
nouvelle photo) pour exercer une issue du verdict :

* erreur_calcul    : l'élève écrit « = n² + 2n » (oubli du +1)  → erreur établie
* lecture_ambigue  : « (2n+1) » en L11 pourrait se lire « (2n-1) » → examen nécessaire
* saut_logique     : hérédité réduite à « ∑ = (n+1)² » sans calcul → examen nécessaire
* formalisation_infidele : l'élève s'est trompé, mais la formalisation « répare »
                     l'erreur (biais classique d'un autoformaliseur) → examen nécessaire

Usage : python generer_variantes.py   (depuis ce répertoire)
"""

import copy
import json
from pathlib import Path

HERE = Path(__file__).parent
FIX = HERE.parent / "fixtures"
TR = json.loads((FIX / "transcription.json").read_text())
ST = json.loads((FIX / "structure.json").read_text())
FM = json.loads((FIX / "formalization.json").read_text())
SUM = "∑ k ∈ Finset.range n, (2 * k + 1)"
SUM_N = r"\sum_{k=0}^{n-1} (2k+1)"


def line(tr, lid):
    return next(x for x in tr["lines"] if x["id"] == lid)


def step(obj, sid, key="id"):
    return next(x for x in obj["steps"] if x[key] == sid)


def save(name, tr, st, fm, issue, desc):
    d = HERE / name
    d.mkdir(exist_ok=True)
    for fn, obj in (("transcription.json", tr), ("structure.json", st), ("formalization.json", fm)):
        (d / fn).write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    (d / "attendu.json").write_text(json.dumps({"issue": issue, "description": desc}, ensure_ascii=False, indent=1))


def with_calc_error(tr, st, fm, formal_repairs=False):
    """L11 : « = n^2 + 2n » au lieu de « = n^2 + (2n+1) »."""
    l11 = line(tr, "p1.L11")
    l11["text"] = r"$= n^2 + 2n$ (d'après H.R.)"
    l11["engine_readings"] = {"lecture-agent": l11["text"]}
    s11, s12 = step(st, "S11"), step(st, "S12")
    s11["statement"] = SUM_N + " + (2n+1) = n^2 + 2n"
    s11["source"][0]["excerpt"] = r"$= n^2 + 2n$ (d'après H.R.)"
    s12["statement"] = "n^2 + 2n = (n+1)^2"
    f11, f12 = step(fm, "S11", "step_id"), step(fm, "S12", "step_id")
    if formal_repairs:
        # La formalisation garde « n ^ 2 + (2 * n + 1) » (ce qui « devrait » être écrit),
        # mais recopie fidèlement le LaTeX de l'élève dans les membres.
        f11["sides"]["rhs_latex"] = "n^2 + 2n"
        f12["sides"]["lhs_latex"] = "n^2 + 2n"
        return
    f11["claim"] = f"{SUM} + (2 * n + 1) = n ^ 2 + 2 * n"
    f11["sides"].update(rhs_lean="n ^ 2 + 2 * n", rhs_latex="n^2 + 2n")
    f12["claim"] = "n ^ 2 + 2 * n = (n + 1) ^ 2"
    f12["sides"].update(lhs_lean="n ^ 2 + 2 * n", lhs_latex="n^2 + 2n")


# 1. Erreur de calcul
tr, st, fm = copy.deepcopy(TR), copy.deepcopy(ST), copy.deepcopy(FM)
with_calc_error(tr, st, fm)
save("erreur_calcul", tr, st, fm, "erreur mathématique établie",
     "L'élève écrit n² + 2n au lieu de n² + (2n+1) : deux égalités fausses, réfutées dans Lean.")

# 2. Formalisation infidèle (répare l'erreur de l'élève)
tr, st, fm = copy.deepcopy(TR), copy.deepcopy(ST), copy.deepcopy(FM)
with_calc_error(tr, st, fm, formal_repairs=True)
save("formalisation_infidele", tr, st, fm, "examen nécessaire",
     "Même copie fausse, mais la formalisation corrige silencieusement l'élève : Lean vérifie tout, "
     "l'empreinte numérique détecte que Lean ne dit pas ce que dit la copie.")

# 3. Lecture ambiguë bloquante
tr, st, fm = copy.deepcopy(TR), copy.deepcopy(ST), copy.deepcopy(FM)
tr["uncertainties"].append({
    "id": "U11", "line_id": "p1.L11", "span": "(2n+1)",
    "readings": [{"text": "(2n+1)", "score": 0.6, "support": ["moteur-A"]},
                 {"text": "(2n-1)", "score": 0.4, "support": ["moteur-B"]}],
    "chosen": "(2n+1)", "reason": "Le signe devant 1 est un trait court : « + » ou « - ».",
    "context_dependent": True})
save("lecture_ambigue", tr, st, fm, "examen nécessaire",
     "Lean vérifie la lecture retenue, mais une autre lecture plausible rendrait l'étape fausse.")

# 4. Saut logique : l'hérédité affirme le résultat sans calcul.
tr, st, fm = copy.deepcopy(TR), copy.deepcopy(ST), copy.deepcopy(FM)
l10 = line(tr, "p1.L10")
l10["text"] = r"$\sum_{k=0}^{n} (2k+1) = (n+1)^2$ (évident)"
l10["engine_readings"] = {"lecture-agent": l10["text"]}
for lid in ("p1.L11", "p1.L12"):
    line(tr, lid)["status"] = "barré"
tr["uncertainties"] = [u for u in tr["uncertainties"] if u["line_id"] not in ("p1.L10",)]
st["steps"] = [s for s in st["steps"] if s["id"] not in ("S11", "S12", "S13")]
s10 = step(st, "S10")
s10.update(kind="affirmation", statement=r"\sum_{k=0}^{n} (2k+1) = (n+1)^2", justification="évident",
           source=[{"line_id": "p1.L10", "excerpt": r"$\sum_{k=0}^{n} (2k+1) = (n+1)^2$ (évident)"}])
step(st, "S14")["depends_on"] = ["S10"]
fm["steps"] = [f for f in fm["steps"] if f["step_id"] not in ("S11", "S12", "S13")]
f10 = step(fm, "S10", "step_id")
f10["claim"] = "∑ k ∈ Finset.range (n + 1), (2 * k + 1) = (n + 1) ^ 2"
f10["sides"].update(rhs_lean="(n + 1) ^ 2", rhs_latex="(n+1)^2")
step(fm, "S14", "step_id")["uses"] = ["S10"]
save("saut_logique", tr, st, fm, "examen nécessaire",
     "L'hérédité affirme le résultat sans utiliser l'hypothèse de récurrence : l'étape est vraie mais "
     "non justifiée ; Lean ne peut ni la vérifier élémentairement ni la réfuter.")
print("variantes générées")
