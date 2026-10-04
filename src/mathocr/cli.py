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
                          arbiter_engine=getattr(a, "arbitre", None),
                          fallback_engine=getattr(a, "secours", None), assembly_engine=getattr(a, "assemblage", None),
                          judge_effort=getattr(a, "effort_juge", "high"),
                          judge_engine=a.juge, tier2=not a.sans_niveau2, polish_feedback=a.reformuler,
                          cache_dir=Path(a.cache), ocr_mode=a.mode_ocr, ocr_strong=a.ocr_fort or [],
                          audit_rate=a.audit, ocr_effort=a.effort_ocr, tier2_engine=getattr(a, "niveau2", None),
                          tolerance=getattr(a, "tolerance", "tolerant"))


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
        p.add_argument("--secours", help="moteur de reprise du niveau 2 ; défaut : --raisonnement")
        p.add_argument("--assemblage", help="moteur d'assemblage du raisonnement ; défaut : --raisonnement")
        p.add_argument("--effort-juge", default="high", choices=["low", "medium", "high"])
        p.add_argument("--arbitre", help="moteur d'arbitrage des lectures douteuses (image) ; défaut : --raisonnement")
        p.add_argument("--juge", help="moteur de rétro-traduction (de préférence un autre fournisseur)")
        p.add_argument("--sans-niveau2", action="store_true")
        p.add_argument("--tolerance", default="tolerant", choices=["tolerant", "strict"],
                       help="tolerant : une justification élémentaire absente (étape vraie) n'empêche pas « vérifié »")
        p.add_argument("--niveau2", help="moteur des preuves de niveau 2 (Lean les contrôle : un modèle bon marché "
                                         "suffit) ; les étapes non tranchées sont retentées par --raisonnement")
        p.add_argument("--reformuler", action="store_true", help="faire reformuler le retour par l'agent")
        p.add_argument("--cache", default=str(ROOT / "runs" / ".cache"))
        p.add_argument("--mode-ocr", default="ensemble", choices=["ensemble", "cascade"],
                       help="cascade : --ocr lisent la page, seules les lignes disputées vont à --ocr-fort")
        p.add_argument("--ocr-fort", action="append", help="moteur(s) de relecture des lignes disputées (cascade)")
        p.add_argument("--audit", type=float, default=0.0, help="part des lignes d'accord relues quand même (0-1)")
        p.add_argument("--effort-ocr", default="high", choices=["low", "medium", "high"],
                       help="effort de raisonnement des lecteurs (coût vs précision)")
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

    ev = sub.add_parser("evaluer", help="mesurer la qualité de lecture d'une configuration OCR sur un jeu de copies")
    ev.add_argument("--jeu", type=Path, default=ROOT / "examples" / "jeu_evaluation.json")
    ev.add_argument("--sortie", type=Path, default=ROOT / "runs" / "evaluation")
    common(ev)

    mo = sub.add_parser("modeles", help="lister les modèles OpenRouter qui lisent les images (prix, schéma strict)")
    mo.add_argument("filtre", nargs="?", default="", help="ex. claude, gemini, gpt, qwen")

    ph = sub.add_parser("photo", help="contrôler la qualité de photos (local, gratuit)")
    ph.add_argument("images", nargs="+", type=Path)

    d = sub.add_parser("demo", help="exemple fourni + variantes (hors-ligne)")
    d.add_argument("--sortie", type=Path, default=ROOT / "runs" / "demo")
    common(d)

    a = ap.parse_args(argv)
    if a.cmd == "modeles":
        from .llm.openrouter_engine import list_vision_models
        try:
            models = list_vision_models()
        except Exception as ex:  # noqa: BLE001
            print(f"OpenRouter injoignable : {ex}")
            return 1
        print(f"{'modèle (à utiliser comme openrouter:<modèle>)':55s} {'entrée $/M':>10s} {'sortie $/M':>10s}  schéma strict")
        for m in models:
            if a.filtre.lower() in m["id"].lower():
                print(f"{m['id']:55s} {m['in']:10.2f} {m['out']:10.2f}  {'oui' if m['structured'] else 'non (repli JSON)'}")
        return 0
    if a.cmd == "photo":
        a.lean_workspace, a.sandbox, a.delai, a.memoire, a.ocr, a.raisonnement = ".", "auto", 1, 1, [], None
        a.juge, a.sans_niveau2, a.reformuler, a.cache, a.verbose = None, True, False, ".", False
        a.mode_ocr, a.ocr_fort, a.audit, a.effort_ocr = "ensemble", [], 0.0, "high"
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = _cfg(a)

    if a.cmd == "evaluer":
        return _evaluate(a, cfg)

    if a.cmd == "photo":
        from .stages.photo_quality import check
        rc = 0
        for p in a.images:
            q = check(p)
            print(f"{p.name} : {'OK' if q.ok else 'REFUSÉE'}  {q.metrics}")
            for pb in q.problems:
                print(f"  ✘ {pb}")
            rc |= 0 if q.ok else 2
        return rc

    if a.cmd == "corriger":
        from .stages.photo_quality import PhotoRejected
        try:
            r = run(a.exercice, a.images, a.sortie, cfg, transcription=a.transcription, structure=a.structure,
                    formalization=a.formalisation)
        except PhotoRejected as ex:
            print(ex)
            return 2
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


def _evaluate(a, cfg) -> int:
    from .llm.pricing import ledger_scope, set_stage
    from .schemas import ReferenceStatement, Transcription
    from .stages.evaluate import score, table
    from .stages.photo_quality import check
    from .stages.transcribe import transcribe

    # Ici on mesure la lecture seule : l'arbitre (s'il est demandé) traite tous les doutes.
    cfg.lazy_judge = False
    jeu = json.loads(a.jeu.read_text())
    base = a.jeu.parent
    label = (f"{cfg.ocr_mode} ({cfg.ocr_effort}): " + " + ".join(cfg.ocr_engines)
             + (f" → {' + '.join(cfg.ocr_strong)}" if cfg.ocr_strong else "")
             + (f" (arbitre {cfg.reasoning_engine})" if cfg.reasoning_engine else ""))
    a.sortie.mkdir(parents=True, exist_ok=True)
    scores = []
    for c in jeu["cas"]:
        ref = ReferenceStatement(**json.loads((base / c["exercice"]).read_text()))
        reference = Transcription(**json.loads((base / c["reference"]).read_text()))
        images = [base / i for i in c["images"]]
        bad = [q for q in map(check, images) if not q.ok]
        if bad:
            print(f"{c['nom']} : photo refusée ({bad[0].problems})")
            continue
        with ledger_scope() as led:
            set_stage("transcription")
            try:
                hyp = transcribe(images, ref, cfg)
            except Exception as ex:  # noqa: BLE001 - une configuration en échec est un résultat
                print(f"| {c['nom']} | {label} | ÉCHEC : {ex} |")
                continue
            cost = led.report()
        (a.sortie / f"{c['nom']}.json").write_text(hyp.model_dump_json(indent=1))
        sc = score(reference, hyp, case=c["nom"], config=label)
        sc.cost_usd = cost.total_usd if cost.complete else None
        scores.append(sc)
        for err in sc.errors:
            print(f"  {c['nom']} {err.ref_line}: attendu « {err.expected} », lu « {err.got} »"
                  f" {'(signalé)' if err.flagged else '(SILENCIEUX)'}")
    print(table(scores))
    (a.sortie / "scores.json").write_text(json.dumps([s.model_dump() for s in scores], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
