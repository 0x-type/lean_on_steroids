/-
  Automatisation *élémentaire* et de confiance du vérificateur MathOCR.

  Chaque étape de la copie est vérifiée par `mathocr_core`, à partir des seules
  hypothèses correspondant aux dépendances déclarées de l'étape. Le niveau de
  puissance est volontairement modeste : calcul (norm_num, ring, omega),
  arithmétique linéaire (linarith), réécriture simple (simp), et le découpage
  du dernier terme d'une somme finie. Une étape qu'aucune de ces tactiques ne
  ferme est « non vérifiée élémentairement » : soit elle est fausse, soit elle
  cache un saut logique — dans les deux cas un humain ou une analyse plus
  poussée doit trancher.

  Chaque alternative laisse une trace `mathocr:closed_by=…` pour que le rapport
  indique *comment* l'étape a été fermée.
-/
import MathOCRCheck.Prelude

macro "mathocr_core" : tactic => `(tactic| first
  | (assumption; trace "mathocr:closed_by=assumption")
  | (rfl; trace "mathocr:closed_by=rfl")
  | (decide; trace "mathocr:closed_by=decide")
  | (omega; trace "mathocr:closed_by=omega")
  | (ring1; trace "mathocr:closed_by=ring")
  | (linarith; trace "mathocr:closed_by=linarith")
  | ((norm_num; done); trace "mathocr:closed_by=norm_num")
  | ((simp; done); trace "mathocr:closed_by=simp")
  | (positivity; trace "mathocr:closed_by=positivity")
  | ((constructor <;> assumption); trace "mathocr:closed_by=constructor")
  | (solve_by_elim (maxDepth := 4); trace "mathocr:closed_by=solve_by_elim")
  | ((simp only [Finset.sum_range_succ, Finset.sum_range_zero, Finset.prod_range_succ,
        Finset.prod_range_zero] at *;
      try (first | omega | ring1 | linarith | (norm_num; done) | (simp_all; done)));
     done; trace "mathocr:closed_by=somme_dernier_terme")
  | ((simp_all; done); trace "mathocr:closed_by=simp_all")
  | (nlinarith; trace "mathocr:closed_by=nlinarith"))

/-- Réfutation d'un énoncé spécialisé : l'hypothèse `h` (instance du contre-exemple)
    doit se réduire à `False` par calcul. -/
macro "mathocr_refute_at " h:ident : tactic => `(tactic| first
  | (norm_num [Finset.sum_range_succ, Finset.prod_range_succ] at $h:ident; done)
  | (simp [Finset.sum_range_succ, Finset.prod_range_succ] at $h:ident; done)
  | (revert $h:ident; decide)
  | (omega))
