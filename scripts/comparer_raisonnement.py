"""Compare des modèles de raisonnement (structure + formalisation) sur des copies dont l'issue est connue.

La lecture est fixée (transcription fournie) et la mémoire partagée désactivée : seul le modèle change.
Usage : python scripts/comparer_raisonnement.py <sortie> <modèle> [<modèle>…]
"""
import json
import sys
import time
from pathlib import Path

from mathocr.pipeline import PipelineConfig, run

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples" / "somme_impairs"
CAS = [
    ("somme_juste", EX / "exercice.json", [EX / "copie_p1.webp"], EX / "fixtures" / "transcription.json",
     "raisonnement vérifié"),
    ("somme_erreur_calcul", EX / "exercice.json", [EX / "copie_p1.webp"],
     EX / "variantes" / "erreur_calcul" / "transcription.json", "erreur mathématique établie"),
    ("somme_saut_logique", EX / "exercice.json", [EX / "copie_p1.webp"],
     EX / "variantes" / "saut_logique" / "transcription.json", "examen nécessaire"),
    ("somme_lecture_ambigue", EX / "exercice.json", [EX / "copie_p1.webp"],
     EX / "variantes" / "lecture_ambigue" / "transcription.json", "examen nécessaire"),
    ("mw07_B_erreur", ROOT / "examples" / "collecte" / "exercices" / "mw07.json",
     [ROOT / "examples" / "collecte" / "photos" / "07-B-erreur-1.jpg"],
     ROOT / "runs" / "collecte_B" / "corr_07" / "1_transcription.json", "pas « raisonnement vérifié »"),
]


def main(out: Path, models: list[str]) -> None:
    rows = []
    for model in models:
        cfg = PipelineConfig(workspace=ROOT / "lean_workspace", reasoning_engine=model,
                             judge_engine="openrouter:google/gemini-3.1-pro-preview", tier2=False,
                             memory_dir=None, cache_dir=ROOT / "runs" / ".cache")
        for name, ex, imgs, tr, expected in CAS:
            if not tr.exists():
                continue
            d = out / model.split("/")[-1] / name
            t0 = time.monotonic()
            try:
                r = run(ex, imgs, d, cfg, transcription=tr)
                got = r.verdict.verdict.value
                ok = got == expected or (expected.startswith("pas") and got != "raisonnement vérifié")
                st = {}
                for c in r.lean.steps:
                    st[c.status] = st.get(c.status, 0) + 1
                fid_bad = sum(1 for c in r.fidelity if c.ok is False)
                rows.append(dict(modele=model, cas=name, attendu=expected, obtenu=got, ok=ok,
                                 cout=round(r.costs.total_usd, 4) if r.costs else None,
                                 secondes=round(time.monotonic() - t0), etapes=st, fidelite_ko=fid_bad))
            except Exception as ex_:  # noqa: BLE001
                rows.append(dict(modele=model, cas=name, attendu=expected, obtenu=f"ÉCHEC : {ex_}", ok=False))
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    (out / f"resultats_{'_'.join(m.split('/')[-1] for m in models)}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2:])
