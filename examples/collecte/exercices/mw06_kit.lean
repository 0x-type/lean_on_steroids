-- Kit du problème 6 : résultats du cours démontrés (recopiés dans mw06.json, revérifiés à chaque correction).
import MathOCRCheck.Prelude
import Mathlib.Topology.Order.IntermediateValue
import Mathlib.Topology.Instances.Real.Lemmas
import Mathlib.Analysis.Real.Sqrt
namespace Kit
theorem valeurs_possibles : ∀ (f : ℝ → ℝ) (a : ℝ), 0 ≤ a → (∀ x, f x ^ 2 = a) → ∀ x, f x = Real.sqrt a ∨ f x = -Real.sqrt a := by
  intro f a ha h x
  have h2 : f x ^ 2 = Real.sqrt a ^ 2 := by rw [h x, Real.sq_sqrt ha]
  exact sq_eq_sq_iff_eq_or_eq_neg.1 h2
theorem image_connexe : ∀ (f : ℝ → ℝ), Continuous f → IsPreconnected (Set.range f) := by
  intro f hf; exact isPreconnected_range hf
theorem connexe_deux_points : ∀ (S : Set ℝ) (u v : ℝ), IsPreconnected S → S ⊆ {u, v} → u ∈ S → v ∈ S → u = v := by
  intro S u v hS hsub hu hv
  by_contra huv
  rcases lt_or_gt_of_ne huv with h | h
  · obtain ⟨w, hw, hw'⟩ := hS.intermediate_value hu hv continuousOn_id (show (u + v) / 2 ∈ Set.Icc (id u) (id v) from ⟨by simp; linarith, by simp; linarith⟩)
    have hm := hsub hw
    simp only [Set.mem_insert_iff, Set.mem_singleton_iff] at hm
    simp only [id] at hw'
    rcases hm with e | e <;> linarith
  · obtain ⟨w, hw, hw'⟩ := hS.intermediate_value hv hu continuousOn_id (show (u + v) / 2 ∈ Set.Icc (id v) (id u) from ⟨by simp; linarith, by simp; linarith⟩)
    have hm := hsub hw
    simp only [Set.mem_insert_iff, Set.mem_singleton_iff] at hm
    simp only [id] at hw'
    rcases hm with e | e <;> linarith
theorem tvi_annulation : ∀ (f : ℝ → ℝ) (x y : ℝ), Continuous f → f x ≤ 0 → 0 ≤ f y → ∃ c, f c = 0 := by
  intro f x y hf hx hy
  obtain ⟨c, hc⟩ := intermediate_value_univ x y hf (show (0:ℝ) ∈ Set.Icc (f x) (f y) from ⟨hx, hy⟩)
  exact ⟨c, hc⟩
end Kit
#print axioms Kit.valeurs_possibles
#print axioms Kit.connexe_deux_points
#print axioms Kit.tvi_annulation
#print axioms Kit.image_connexe
