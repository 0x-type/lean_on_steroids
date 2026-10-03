"""Interface en ligne de commande.

    mathocr corriger --exercice ex.json --images p1.jpg p2.jpg --sortie runs/eleve42 \\
        --ocr anthropic:claude-opus-5-5 --ocr gemini:gemini-3-pro --ocr mathpix \\
        --raisonnement anthropic:claude-opus-5-5 --juge openai:gpt-5

    mathocr demo            # exemple fourni + variantes, sans clé d'API
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .pipeline import PipelineConfig, run
from .schemas import RunResult

ROOT = Path(__file__).resolve().parents[2]


def _summary(r: RunResult, out: Path) -> str:
    v = r.verdict
    lines = [f"Issue : {v.verdict.value.upper()}"]
    lines += [f"  • {x}" for x in v.reasons]
    if v.blocking_issues:
        lines.append("Points bloquants :")
        lines += [f"  ✘ {x}" for x in v.blocking_issues]
    counts: dict[str, int] = {}
    for c in r.lean.steps:
        counts[c.status] = counts.get(c.status, 0) + 1
    lines.append("Étapes : " + ", ".join(f"{k}={n}" for k, n in sorted(counts.items())))
    if r.lean.run:
        ver = re.search(r"version ([\w.\-]+)", r.lean.run.lean_version)
        lines.append(f"Lean {ver.group(1) if ver else '?'} · isolation {r.lean.run.sandbox} · "
                     f"{r.lean.run.seconds:.1f} s")
    if r.costs is not None:
        c = r.costs
        lines.append(f"Coût : {c.total_usd:.4f} $" + ("" if c.complete else " (+ appels sans prix connu)")
                     + f" · {c.llm_calls} appel(s) aux modèles, {c.cached_calls} servi(s) par le cache")
    lines.append(f"Retour : {r.feedback.summary}")
    lines.append(f"Rapport : {out / 'rapport.html'}")
    return "\n".join(lines)


def _cfg(a) -> PipelineConfig:
    return PipelineConfig(workspace=Path(a.lean_workspace), sandbox=a.sandbox, lean_timeout_s=a.delai,
                          lean_memory_mb=a.memoire, ocr_engines=a.ocr or [], reasoning_engine=a.raisonnement,
                          judge_engine=a.juge, tier2=not a.sans_niveau2, polish_feedback=a.reformuler,
                          cache_dir=Path(a.cache))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mathocr", description="Correction de copies manuscrites vérifiée par Lean 4.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--lean-workspace", default=str(ROOT / "lean_workspace"))
        p.add_argument("--sandbox", default="auto", choices=["auto", "unshare", "docker", "local"])
        p.add_argument("--delai", type=float, default=300.0, help="délai maximal de Lean (s)")
        p.add_argument("--memoire", type=int, default=8192, help="mémoire maximale de Lean (Mo)")
        p.add_argument("--ocr", action="append", help="moteur OCR (répétable) : anthropic:…, openai:…, gemini:…, mathpix")
        p.add_argument("--raisonnement", help="moteur des agents (structure, formalisation, arbitrage, niveau 2)")
        p.add_argument("--juge", help="moteur de rétro-traduction (de préférence un autre fournisseur)")
        p.add_argument("--sans-niveau2", action="store_true")
        p.add_argument("--reformuler", action="store_true", help="faire reformuler le retour par l'agent")
        p.add_argument("--cache", default=str(ROOT / "runs" / ".cache"))
        p.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("corriger", help="corriger une copie")
    c.add_argument("--exercice", required=True, type=Path)
    c.add_argument("--images", nargs="+", type=Path, default=[])
    c.add_argument("--sortie", required=True, type=Path)
    c.add_argument("--transcription", type=Path, help="transcription fournie (fixture ou corrigée à la main)")
    c.add_argument("--structure", type=Path)
    c.add_argument("--formalisation", type=Path)
    c.add_argument("--json", action="store_true", help="afficher le résultat complet en JSON")
    common(c)

    d = sub.add_parser("demo", help="exemple fourni + variantes (hors-ligne)")
    d.add_argument("--sortie", type=Path, default=ROOT / "runs" / "demo")
    common(d)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = _cfg(a)

    if a.cmd == "corriger":
        r = run(a.exercice, a.images, a.sortie, cfg, transcription=a.transcription, structure=a.structure,
                formalization=a.formalisation)
        print(r.model_dump_json(indent=1) if a.json else _summary(r, a.sortie))
        return 0

    ex = ROOT / "examples" / "somme_impairs"
    cases = [("copie", ex / "fixtures")] + sorted((p.name, p) for p in (ex / "variantes").iterdir() if p.is_dir())
    rc = 0
    for name, d_ in cases:
        expected = json.loads((d_ / "attendu.json").read_text())["issue"] if (d_ / "attendu.json").exists() else None
        out = a.sortie / name
        r = run(ex / "exercice.json", [ex / "copie_p1.webp"], out, cfg, transcription=d_ / "transcription.json",
                structure=d_ / "structure.json", formalization=d_ / "formalization.json")
        ok = expected is None or r.verdict.verdict.value == expected
        rc |= 0 if ok else 1
        print(f"=== {name} {'(attendu : ' + expected + ')' if expected else ''} {'✔' if ok else '✘'}")
        print(_summary(r, out))
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
