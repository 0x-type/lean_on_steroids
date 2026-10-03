"""Sélection d'un jeu de collecte varié dans le catalogue Math Woods, avec la solution acceptée."""
import json
import re
import sys
import time
import urllib.request

from bs4 import BeautifulSoup

GROUPES = {
    "Arithmétique": ["Arithmetic", "Number theory"],
    "Algèbre générale": ["General algebra", "Group", "Ring", "Field", "Polynomial"],
    "Algèbre linéaire": ["Linear algebra", "Reduction of endomorphism", "Euclidean vector space"],
    "Analyse": ["Real analysis", "Sequence and series", "Real function", "Riemann integration"],
    "Géométrie": ["Geometry"],
    "Combinatoire et probabilités": ["Combinatorics", "Discrete mathematics", "Probability on finite space",
                                     "Probability and statistics", "Graph theory", "Enigma"],
    "Ensembles et logique": ["Set theory", "Mathematical formalism", "Mathematical logic"],
}
QUOTA = {"Arithmétique": 4, "Algèbre générale": 3, "Algèbre linéaire": 4, "Analyse": 5, "Géométrie": 3,
         "Combinatoire et probabilités": 3, "Ensembles et logique": 2}
FR = re.compile(r"\b(Montrer|Montrez|Soit|soit|Démontrer|Prouver|pour tout|On considère|Déterminer|Calculer)\b")
NOTATION = [r"\\begin\{pmatrix|\\begin\{bmatrix", r"\\int", r"\\lim", r"\\sum|\\prod", r"\\forall|\\exists",
            r"\\in|\\subset|\\cup|\\cap", r"\\alpha|\\beta|\\lambda|\\varepsilon|\\theta|\\sigma", r"\\frac",
            r"\\sqrt", r"\\equiv|\\mid", r"\\det|\\mathrm\{tr\}|\\ker"]


def solution(url: str) -> tuple[str, str]:
    html = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "MathOCR-recherche/0.1"}),
                                  timeout=40).read().decode("utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    for k in soup.select(".katex"):
        ann = k.find("annotation")
        k.replace_with(f" ${ann.get_text().strip()}$ " if ann else k.get_text())
    lines = [l.strip() for l in (soup.find("main") or soup).get_text("\n").splitlines() if l.strip()]
    if "Solution by" not in lines:
        return "", ""
    i = lines.index("Solution by")
    author = lines[i + 1]
    j = i + 2
    while j < len(lines) and lines[j] in ("FR", "EN", "Discussions") or re.fullmatch(r"\d+ useful votes?", lines[j] if j < len(lines) else ""):
        j += 1
    k = next((m for m in range(j, len(lines)) if lines[m] in ("Solution by", "Report") or lines[m].startswith("Add a solution")), len(lines))
    sol = re.sub(r"\s+", " ", " ".join(lines[j:k])).strip()
    sol = re.sub(r"^(\d+\s+)?\d+\s+useful votes?\s*", "", sol)
    return author, sol


def main(cat_path: str, out: str) -> None:
    cat = json.load(open(cat_path))
    uniq = {p["statement"][:200]: p for p in cat if p.get("statement")}.values()
    picked = []
    for groupe, doms in GROUPES.items():
        cands = [p for p in uniq if p["domain"] in doms and (p["solutions"] or 0) > 0 and p["reviewed"]
                 and p["difficulty"] and 8 <= p["difficulty"] <= 50 and len(p["statement"]) <= 700
                 and FR.search(p["statement"])]
        # privilégier la variété de notation, puis la difficulté croissante
        cands.sort(key=lambda p: (-sum(bool(re.search(n, p["statement"])) for n in NOTATION), p["difficulty"]))
        got = 0
        for p in cands:
            if got >= QUOTA[groupe]:
                break
            time.sleep(0.5)
            try:
                author, sol = solution(p["url"])
            except Exception:  # noqa: BLE001
                continue
            if not (150 <= len(sol) <= 1500) or not FR.search(sol + " Soit"):
                continue
            if re.search(r"flemme|trivial|aisément|évident\b|laissé au lecteur|Report For", sol, re.I):
                continue  # preuve incomplète ou trop allusive : pas un modèle à recopier
            picked.append({**p, "groupe": groupe, "solution_auteur": author, "solution": sol})
            got += 1
            print(f"{groupe:30s} {p['difficulty']:3d}  {p['title'][:60]}", flush=True)
    json.dump(picked, open(out, "w"), ensure_ascii=False, indent=1)
    print(len(picked), "problèmes retenus")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
