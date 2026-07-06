from devagent.cli import build_pipeline_phases


def test_brain_only_phases():
    phases, gates = build_pipeline_phases("prd.md")
    assert [p.name for p in phases] == ["scope", "plan"]
    assert set(gates) == {"scope", "plan"}


def test_build_phases_include_build_and_deploy():
    class _Ex:  # stand-in executor
        def build(self, req): ...
    phases, gates = build_pipeline_phases(
        "prd.md", build=True, out_dir="/tmp/out", run_id="r1", executor=_Ex(), verifier=None)
    assert [p.name for p in phases] == ["scope", "plan", "build", "deploy"]
    assert set(gates) == {"scope", "plan", "build", "deploy"}


def test_build_deploy_false_omits_deploy():
    class _Ex:  # stand-in executor
        def build(self, req): ...
    phases, gates = build_pipeline_phases(
        "prd.md", build=True, deploy=False, out_dir="/tmp/out", run_id="r1",
        executor=_Ex(), verifier=None)
    assert [p.name for p in phases] == ["scope", "plan", "build"]
    assert set(gates) == {"scope", "plan", "build"}


def test_scope_param_freezes_the_scope_phase():
    # One-flow: a precomputed scope (from the architect's design) replaces the LLM ScopePhase —
    # the sub-build must not re-decide the design.
    from devagent.cli import build_pipeline_phases
    from devagent.phases.scope import FrozenScopePhase
    from devagent.schema import AcceptanceCheck, ArtifactSpec, ProjectScope
    scope = ProjectScope(title="frozen", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])])
    phases, gates = build_pipeline_phases("ignored.md", scope=scope)
    assert isinstance(phases[0], FrozenScopePhase)
    res = phases[0].run(None)
    assert res.exit_code == 0 and res.output_artifact is scope


def test_frozen_plan_phase_emits_the_given_plan_without_llm():
    from devagent.phases.plan import FrozenPlanPhase
    from devagent.schema import Plan, Task
    plan = Plan(tasks=[Task(id="t1", description="scaffold", owned_files=["api/index.js"])])
    phase = FrozenPlanPhase(plan)
    assert phase.name == "plan"
    res = phase.run(None)
    assert res.exit_code == 0 and res.output_artifact is plan


def test_plan_param_freezes_the_plan_phase():
    from devagent.cli import build_pipeline_phases
    from devagent.phases.plan import FrozenPlanPhase
    from devagent.schema import (AcceptanceCheck, ArtifactSpec, Plan, ProjectScope, Task)
    scope = ProjectScope(title="frozen", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])])
    plan = Plan(tasks=[Task(id="t1", description="x", owned_files=["api/index.js"])])

    class _Ex:
        def build(self, req): ...
    phases, gates = build_pipeline_phases(
        "ignored.md", build=True, deploy=False, out_dir="/tmp/out", run_id="r1",
        executor=_Ex(), verifier=None, scope=scope, plan=plan)
    assert [p.name for p in phases] == ["scope", "plan", "build"]
    assert isinstance(phases[1], FrozenPlanPhase)
    assert phases[1].run(None).output_artifact is plan


def test_repair_context_forwards_to_build_phase():
    from devagent.cli import build_pipeline_phases
    from devagent.schema import (AcceptanceCheck, ArtifactSpec, ProjectScope)
    scope = ProjectScope(title="frozen", targets=[
        ArtifactSpec(type="backend", stack="node-express", name="api",
                     acceptance_checks=[AcceptanceCheck(kind="route_status", route="/")])])

    class _Ex:
        def build(self, req): ...
    phases, _ = build_pipeline_phases(
        "ignored.md", build=True, deploy=False, out_dir="/tmp/out", run_id="r1",
        executor=_Ex(), verifier=None, scope=scope, repair_context="INTEGRATION FAILED: ...")
    build_phase = next(p for p in phases if p.name == "build")
    assert build_phase.repair_context == "INTEGRATION FAILED: ..."
