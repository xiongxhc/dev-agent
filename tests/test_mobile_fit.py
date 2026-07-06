"""mobile_fit check — the mechanical floor under "redesign for mobile, don't squeeze":
a desktop layout crammed into a phone viewport must FAIL verification (horizontal overflow,
sub-44px touch targets), so the repair loop restructures instead of shrinking."""

from devagent.acceptance_runner import _mobile_fit_verdict
from devagent.schema import AcceptanceCheck, ArtifactSpec, ProjectScope
import pytest


def test_verdict_passes_clean_mobile_layout():
    ok, detail = _mobile_fit_verdict(390, 390, [])
    assert ok and "no horizontal overflow" in detail


def test_verdict_fails_horizontal_overflow():
    ok, detail = _mobile_fit_verdict(742, 390, [])
    assert not ok and "742" in detail and "390" in detail


def test_verdict_fails_small_touch_targets():
    ok, detail = _mobile_fit_verdict(390, 390, ["button 'Save' 28px", "button 'Del' 30px"])
    assert not ok and "Save" in detail


def test_schema_mobile_fit_requires_route():
    AcceptanceCheck(kind="mobile_fit", route="/")             # valid
    with pytest.raises(Exception):
        AcceptanceCheck(kind="mobile_fit")                    # no route -> invalid


def test_frontend_recipe_supports_mobile_fit():
    from devagent import recipes
    assert "mobile_fit" in recipes.get("node-vite-react").supported_checks


def test_scope_gate_accepts_mobile_fit_on_frontend():
    from devagent.phase_gates import ScopeGate
    from devagent.phases.base import PhaseResult
    scope = ProjectScope(title="t", targets=[
        ArtifactSpec(type="frontend", stack="node-vite-react", name="web",
                     acceptance_checks=[AcceptanceCheck(kind="mobile_fit", route="/")])])
    assert ScopeGate().check(PhaseResult(name="scope", exit_code=0, output_artifact=scope)).ok
