"""Mémoire partagée par exercice : ne payer qu'une fois pour une même étape.

Deux types d'entrées, toutes deux réutilisables d'une copie (ou d'un élève)
à l'autre sans changer le résultat :

* **traductions d'étapes produites par un agent**, enregistrées seulement après
  avoir passé les contrôles de fidélité (empreinte numérique, prédicats…) et
  compilé dans Lean. Clé : exercice + nature de l'étape + formule normalisée +
  variables de la portée et leurs types + définitions de l'élève.
* **preuves / réfutations de niveau 2**, clé : l'énoncé Lean exact (hypothèses
  comprises). Elles sont de toute façon recompilées par Lean à chaque usage.

Une entrée invalide ne peut donc pas se propager : la mémoire ne contient que
ce qui a été vérifié, et tout ce qui en sort est revérifié.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .schemas import StepFormal
from .stages.fidelity import norm


def _key(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


class StepMemory:
    def __init__(self, root: Path, exercise_id: str):
        self.dir = Path(root) / exercise_id
        (self.dir / "etapes").mkdir(parents=True, exist_ok=True)
        (self.dir / "niveau2").mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.stored = 0

    @staticmethod
    def step_key(kind: str, statement: str, scope_vars: list[tuple[str, str]], defs: list[str]) -> str:
        return _key({"kind": kind, "statement": norm(statement), "vars": sorted(scope_vars), "defs": sorted(defs)})

    def get_step(self, key: str) -> StepFormal | None:
        p = self.dir / "etapes" / f"{key}.json"
        if not p.exists():
            return None
        self.hits += 1
        return StepFormal(**json.loads(p.read_text())["formal"])

    def put_step(self, key: str, f: StepFormal, statement: str) -> None:
        p = self.dir / "etapes" / f"{key}.json"
        data = f.model_dump(exclude={"uses", "agent_proof", "agent_refutation"})
        data["origin"] = "mémoire"
        p.write_text(json.dumps({"statement": statement, "formal": data}, ensure_ascii=False, indent=1))
        self.stored += 1

    @staticmethod
    def tier2_key(binders: str, claim: str) -> str:
        return _key({"binders": binders, "claim": claim})

    def get_tier2(self, key: str) -> dict | None:
        p = self.dir / "niveau2" / f"{key}.json"
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())
        return None

    def put_tier2(self, key: str, proof: str | None, refutation: str | None) -> None:
        (self.dir / "niveau2" / f"{key}.json").write_text(
            json.dumps({"proof": proof, "refutation": refutation}, ensure_ascii=False, indent=1))
        self.stored += 1
