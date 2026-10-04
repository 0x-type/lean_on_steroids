import pytest

from leanonsteroids.lean.policy import check_axioms, check_fragment


@pytest.mark.parametrize("frag", [
    "by sorry",
    "(by admit)",
    "axiom foo : False",
    "by native_decide",
    "Lean.ofReduceBool x",
    "#eval 1",
    "set_option maxHeartbeats 0 in True",
    "True -- commentaire",
    "True /- x -/",
    "True) theorem evil : False := by sorry",
    "@[simp] True",
    "by run_tac pure ()",
    "unsafe_cast",  # identifiant libre mais inoffensif : doit passer
])
def test_forbidden_fragments(frag):
    v = check_fragment(frag, "test")
    if frag == "unsafe_cast":
        assert v == []
    else:
        assert v, f"le fragment devrait être refusé : {frag}"


def test_allowed_fragment():
    assert check_fragment("∑ k ∈ Finset.range (n + 1), (2 * k + 1) = (n + 1) ^ 2", "t") == []


def test_newline_rejected_in_terms_but_allowed_in_proofs():
    assert check_fragment("a = b\nc", "t")
    assert not check_fragment("intro h\nomega", "t", multiline=True)


def test_axioms():
    assert check_axioms({"x": ["propext", "Classical.choice", "Quot.sound"]}) == []
    assert check_axioms({"x": ["propext", "sorryAx"]})
    assert check_axioms({"x": ["Lean.ofReduceBool"]})


def test_greek_identifiers_allowed_except_lambda():
    from leanonsteroids.lean.policy import check_identifier
    assert not check_identifier("α", "t") and not check_identifier("ε₁", "t")
    assert check_identifier("λ", "t") and check_identifier("sorry", "t")


def test_agent_tactic_block_keeps_its_layout():
    """Cas réel (B06) : une preuve juste, uniformément indentée ou commencée par « by », était cassée par la
    mise en page et refusée par Lean."""
    from leanonsteroids.lean.leangen import _tactics
    assert _tactics("  have h : 1 = 1 := by\n    rfl\n  exact h") == "  have h : 1 = 1 := by\n    rfl\n  exact h"
    assert _tactics("by\n  intro x\n  rfl") == "  intro x\n  rfl"
    assert _tactics("intro x\nrfl\n") == "  intro x\n  rfl"
