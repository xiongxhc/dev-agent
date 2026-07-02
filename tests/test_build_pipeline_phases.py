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
