-- Preuve que la formalisation de référence de mw07.json est un énoncé vrai (axiomes standard seulement).
-- Vérifier : cd lean_workspace && lake env lean ../examples/collecte/exercices/mw07_reference.lean
import MathOCRCheck.Prelude
import Mathlib.Analysis.SpecificLimits.Basic
import Mathlib.Topology.UniformSpace.UniformConvergence
open Filter Topology
theorem ref7 : (∀ x ∈ Set.Icc (0:ℝ) 1, Tendsto (fun n : ℕ => x ^ n) atTop (𝓝 (if x = 1 then 1 else 0))) ∧
    ¬ TendstoUniformlyOn (fun (n : ℕ) (x : ℝ) => x ^ n) (fun x => if x = 1 then 1 else 0) atTop (Set.Icc 0 1) ∧
    ∀ α : ℝ, 0 < α → α < 1 →
      TendstoUniformlyOn (fun (n : ℕ) (x : ℝ) => x ^ n) (fun x => if x = 1 then 1 else 0) atTop (Set.Icc 0 α) := by
  refine ⟨?_, ?_, ?_⟩
  · intro x hx
    by_cases h : x = 1
    · subst h; simp
    · simp only [h, ite_false]
      exact tendsto_pow_atTop_nhds_zero_of_lt_one hx.1 (lt_of_le_of_ne hx.2 h)
  · intro hU
    rw [Metric.tendstoUniformlyOn_iff] at hU
    obtain ⟨n, hn⟩ := (hU (1/2) (by norm_num)).exists
    set t : ℝ := 1 / (2 * ((n:ℝ) + 1)) with ht
    have ht0 : 0 < t := by positivity
    have ht1 : t ≤ 1 / 2 := by
      rw [ht, div_le_div_iff₀ (by positivity) (by norm_num)]; nlinarith [(Nat.cast_nonneg n : (0:ℝ) ≤ n)]
    have hx : (1 - t) ∈ Set.Icc (0:ℝ) 1 := ⟨by linarith, by linarith⟩
    have hne : (1 - t) ≠ 1 := by intro h; linarith
    have key := hn (1 - t) hx
    simp only [hne, ite_false, Real.dist_eq, zero_sub, abs_neg] at key
    have bern := one_add_mul_le_pow (show (-2:ℝ) ≤ -t by linarith) n
    have hnt : (n:ℝ) * t < 1 / 2 := by
      rw [ht]; rw [mul_one_div, div_lt_iff₀ (by positivity)]; nlinarith
    have hpos : 0 ≤ (1 - t) ^ n := pow_nonneg (by linarith) n
    rw [abs_of_nonneg hpos] at key
    have e1 : (1:ℝ) + n * -t = 1 - n * t := by ring
    have e2 : (1:ℝ) + -t = 1 - t := by ring
    rw [e1, e2] at bern
    linarith
  · intro α hα0 hα1
    rw [Metric.tendstoUniformlyOn_iff]
    intro ε hε
    have hlim := tendsto_pow_atTop_nhds_zero_of_lt_one hα0.le hα1
    filter_upwards [(hlim.eventually (gt_mem_nhds hε))] with n hn x hx
    have hne : x ≠ 1 := by intro h; rw [h] at hx; linarith [hx.2]
    simp only [hne, ite_false, Real.dist_eq, zero_sub, abs_neg]
    rw [abs_of_nonneg (pow_nonneg hx.1 n)]
    exact lt_of_le_of_lt (pow_le_pow_left₀ hx.1 hx.2 n) hn
#print axioms ref7
