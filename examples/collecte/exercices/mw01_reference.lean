-- Preuve que la formalisation de référence de mw01.json est un énoncé vrai (axiomes standard seulement).
import MathOCRCheck.Prelude
import Mathlib.NumberTheory.ArithmeticFunction.Misc
open Finset
theorem ref1 : ∀ a b : ℕ, 0 < a → 0 < b → Nat.Coprime a b →
    ∑ d ∈ (a * b).divisors, d = (∑ d ∈ a.divisors, d) * (∑ d ∈ b.divisors, d) := by
  intro a b _ _ h
  have := (ArithmeticFunction.isMultiplicative_sigma (k := 1)).map_mul_of_coprime h
  simpa [ArithmeticFunction.sigma_apply] using this
#print axioms ref1
