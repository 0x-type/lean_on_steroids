"""Catalogue des problèmes de Math Woods (https://mathwoods.org) : titre, domaine, difficulté,
énoncé (LaTeX), nombre de solutions publiques. Lecture polie (robots.txt respecté, 0,4 s entre
deux pages). Contenu sous licence CC BY-NC-SA 4.0 : attribution obligatoire, usage non commercial.

Usage : python catalogue_mathwoods.py SORTIE.json
"""
import json
import re
import sys
import time
import urllib.request

from bs4 import BeautifulSoup

UA = {"User-Agent": "Lean On Steroids-recherche/0.1 (prototype non commercial)"}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def parse(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for k in soup.select(".katex"):
        ann = k.find("annotation")
        k.replace_with(f" ${ann.get_text().strip()}$ " if ann else k.get_text())
    main = soup.find("main") or soup
    lines = [l.strip() for l in main.get_text("\n").splitlines() if l.strip()]
    title = (main.find("h1").get_text(" ", strip=True) if main.find("h1") else "")
    domain = ""
    if "Problems" in lines:
        i = lines.index("Problems")
        if i + 2 < len(lines) and lines[i + 1] == "/":
            domain = lines[i + 2]
    diff = None
    for i, l in enumerate(lines[:40]):
        if l == "by" and i + 3 < len(lines) and lines[i + 2] == "·" and lines[i + 3].isdigit():
            diff = int(lines[i + 3])
            break
    end = lines.index("I solved it") if "I solved it" in lines else len(lines)
    start = 0
    for marker in ("Add that translation", "Add translation"):
        if marker in lines[:end]:
            start = max(start, lines.index(marker) + 1)
    if start and start < end and lines[start] == ".":
        start += 1
    statement = re.sub(r"\s+", " ", " ".join(lines[start:end])).strip()
    sols = None
    if "Solutions" in lines:
        j = len(lines) - 1 - lines[::-1].index("Solutions")
        if j + 1 < len(lines) and lines[j + 1].isdigit():
            sols = int(lines[j + 1])
    lang = "fr" if "Français" in lines[:60] and "Showing the Français version" in " ".join(lines[:80]) else ""
    return {"url": url, "title": title, "domain": domain, "difficulty": diff, "solutions": sols,
            "reviewed": "Reviewed" in lines[:80], "lang_hint": lang, "statement": statement}


def main(out: str) -> None:
    urls = sorted(set(re.findall(r"<loc>(https://mathwoods.org/problems/[^<]+)</loc>",
                                 get("https://mathwoods.org/sitemap.xml"))))
    urls = [u for u in urls if not u.endswith("/new")]
    done = {}
    try:
        done = {p["url"]: p for p in json.load(open(out))}
    except Exception:  # noqa: BLE001
        pass
    for n, u in enumerate(urls, 1):
        if u in done:
            continue
        try:
            done[u] = parse(u, get(u))
        except Exception as ex:  # noqa: BLE001
            done[u] = {"url": u, "error": str(ex)}
        if n % 50 == 0:
            json.dump(list(done.values()), open(out, "w"), ensure_ascii=False, indent=1)
            print(f"{n}/{len(urls)}", flush=True)
        time.sleep(0.4)
    json.dump(list(done.values()), open(out, "w"), ensure_ascii=False, indent=1)
    print(f"terminé : {len(done)} pages", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
