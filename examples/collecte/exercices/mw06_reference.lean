-- Preuve que la formalisation de référence de mw06.json est un énoncé vrai (axiomes standard seulement).
import MathOCRCheck.Prelude
import Mathlib.Topology.Order.IntermediateValue
import Mathlib.Topology.Instances.Real.Lemmas
theorem ref6 : ∀ (f : ℝ → ℝ) (a : ℝ), Continuous f → 0 ≤ a → (∀ x, f x ^ 2 = a) → ∀ x y, f x = f y := by
  intro f a hf ha h x y
  by_contra hne
  have hsq : f x ^ 2 = f y ^ 2 := by rw [h x, h y]
  have hneg : f x = - f y := by
    rcases sq_eq_sq_iff_eq_or_eq_neg.1 hsq with e | e
    · exact absurd e hne
    · exact e
  have hy0 : f y ≠ 0 := by intro h0; apply hne; rw [hneg, h0]; simp
  obtain ⟨c, hc⟩ : ∃ c, f c = 0 := by
    rcases lt_or_gt_of_ne hy0 with hlt | hgt
    · obtain ⟨c, hc⟩ := intermediate_value_univ y x hf (show (0:ℝ) ∈ Set.Icc (f y) (f x) from ⟨by linarith, by linarith⟩)
      exact ⟨c, hc⟩
    · obtain ⟨c, hc⟩ := intermediate_value_univ x y hf (show (0:ℝ) ∈ Set.Icc (f x) (f y) from ⟨by linarith, by linarith⟩)
      exact ⟨c, hc⟩
  have ha0 : a = 0 := by rw [← h c, hc]; norm_num
  have hy2 := h y
  rw [ha0] at hy2
  exact hy0 (pow_eq_zero_iff (n := 2) (by norm_num) |>.mp hy2)
#print axioms ref6
