/-
  Prélude Mathlib utilisé par le vérificateur MathOCR.

  Le cache binaire de Mathlib n'étant pas toujours téléchargeable (réseau
  restreint), on compile seulement le sous-ensemble utile aux exercices de
  niveau prépa / licence 1. Remplacer par `import Mathlib` quand la
  bibliothèque complète est disponible (voir `mathocr.toml`, clé `lean.imports`).
-/
import Mathlib.Tactic.Common
import Mathlib.Tactic.Ring
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.Positivity
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.IntervalCases
import Mathlib.Tactic.Bound
import Mathlib.Algebra.BigOperators.Group.Finset.Basic
import Mathlib.Algebra.BigOperators.Intervals
import Mathlib.Algebra.BigOperators.Ring.Finset
import Mathlib.Algebra.Group.Nat.Even
import Mathlib.Algebra.Order.Field.Basic
import Mathlib.Data.Nat.Factorial.Basic
import Mathlib.Data.Int.GCD
import Mathlib.Basic.Real.Basic
