# Espace Lean de Lean On Steroids

* `MathOCRCheck/Prelude.lean` : sous-ensemble de Mathlib importé par les fichiers générés
  (remplaçable par `import Mathlib` si la bibliothèque complète est compilée ou téléchargée).
* `MathOCRCheck/Auto.lean` : automatisation élémentaire de confiance (`mathocr_core`,
  `mathocr_refute_at`). C'est la seule « intelligence » autorisée à prouver une étape de copie.

Installation (Lean 4.34.1, Mathlib v4.34.1) :

```sh
curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none
cd lean_workspace
lake exe cache get          # cache binaire Mathlib (si le réseau l'autorise)
lake build MathOCRCheck.Prelude MathOCRCheck.Auto   # ~15 min sans cache sur 4 cœurs
```
