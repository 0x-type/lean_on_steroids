-- Preuve que la formalisation de référence de mw10.json est un énoncé vrai (axiomes standard seulement).
import MathOCRCheck.Prelude
open Finset
theorem ref10 : ∀ n : ℕ, ∑ k ∈ Finset.range (n + 1), (k : ℚ) = (n : ℚ) * ((n : ℚ) + 1) / 2 := by
  intro n
  induction n with
  | zero => simp
  | succ m ih => rw [Finset.sum_range_succ, ih]; push_cast; ring
#print axioms ref10
