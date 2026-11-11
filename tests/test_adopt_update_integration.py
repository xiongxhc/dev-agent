"""The adopt → update hand-off: `update-system <adopt-dir> <change.md>` on an ADOPTED run dir.

The synthetic run dir exists so that changing an adopted system needs no new plumbing: adopt
emits `design.json` + `services/<name>/`, and `update_system` consumes them without knowing who
wrote them. Testing the two halves separately would not show that the artifact one writes is
the artifact the other reads, so these tests run them end to end.

No Docker and no tokens: `update_system` injects every side-effecting piece (architect,
run_node, bring_up_factory, integration_runner, reap), so the seam under test here is the
artifact contract, not the build machinery.
"""

import json

import pytest

from devagent.adopt import adopt
from devagent.adopt.prove import UNMAPPABLE, Verdict
from devagent.budget import Budget
from devagent.contract_utils import derive_integration_checks
from devagent.design_diff import load_design
from devagent.ledger import Ledger
from devagent.integration import IntegrationReport
from devagent.schema import SystemDesign
from devagent.system_build import update_system


def _fixture_repo(tmp_path):
    """A two-service repo: one Maven module, one Vite/pnpm frontend."""
    repo = tmp_path / "src-repo"
    (repo / "api/src/main/java").mkdir(parents=True)
    (repo / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>root</artifactId>'
        "<packaging>pom</packaging><properties><java.version>8</java.version></properties>"
        "<modules><module>api</module></modules></project>")
    (repo / "api/pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>api</artifactId>'
        "<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>")
    (repo / "api/src/main/java/App.java").write_text("@SpringBootApplication class App {}")
    (repo / "web").mkdir()
    (repo / "web/package.json").write_text(json.dumps(
        {"name": "web", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}))
    (repo / "web/pnpm-lock.yaml").write_text("")
    return repo


def _adopt(tmp_path, name="adopted"):
    """Adopt the fixture with the map stage skipped and prove stubbed — the free path."""
    out = tmp_path / name
    result = adopt(_fixture_repo(tmp_path), out,
                   prover=lambda d, rd, **kw: [Verdict(n.name, UNMAPPABLE, "stubbed")
                                               for n in d.services])
    assert result.status == "adopted", result.detail
    return out, result


@pytest.fixture
def updated():
    """Collects what the update actually asked the builder to do."""
    return {"built": [], "brought_up": [], "preserve_data": [], "checked": []}


def _fakes(updated, *, change_service="web"):
    """Injected collaborators. The architect returns the prior design with ONE service's slice
    changed, so the mechanical diff — not a model — decides the rebuild set."""
    def architect(change_path, prior):
        data = json.loads(prior.model_dump_json())
        for svc in data["services"]:
            if svc["name"] == change_service:
                svc["prd_slice"] = "CHANGED: " + svc["prd_slice"]
        return SystemDesign.model_validate(data)

    def run_node(node, design, repair_context=None):
        from devagent.tree import SUCCEEDED
        from devagent.system_build import NodeResult
        updated["built"].append(node.name)
        return NodeResult(node.id, SUCCEEDED, "rebuilt")

    def bring_up_factory(preserve_data):
        updated["preserve_data"].append(preserve_data)

        def bring_up(design):
            updated["brought_up"].append([s.name for s in design.services])
            return {s.id: f"http://127.0.0.1:900{i}" for i, s in enumerate(design.services)}, \
                lambda: None
        return bring_up

    return architect, run_node, bring_up_factory


def _probe(updated):
    """A stand-in for the real HTTP runner: it reports one passing step per DERIVED check. It
    must not return zero steps for a non-empty check set — a report that proves nothing is
    exactly what IntegrationGate is built to reject, so a fake that shortcuts it would test the
    gate instead of the hand-off."""
    def runner(checks, base_urls):
        updated["checked"].append([(c.service, c.route) for c in checks])
        return IntegrationReport(steps=[{"service": c.service, "route": c.route, "ok": True,
                                         "detail": "probed (fake)"} for c in checks])
    return runner


def _run_update(out, change, updated, **kw):
    architect, run_node, bring_up_factory = _fakes(updated, **kw)
    return update_system(
        out, str(change), budget=Budget(1_000_000, 1800.0, 3), ledger=Ledger(out),
        run_node=run_node, bring_up_factory=bring_up_factory, architect=architect,
        integration_runner=_probe(updated), reap=lambda *a: None)


# ---------------------------------------------------------------- the hand-off

def test_update_system_consumes_an_adopt_run_dir(tmp_path):
    """The design's central promise, executed: adopt's artifacts drive an update with no
    conversion step in between."""
    out, _ = _adopt(tmp_path)
    change = tmp_path / "change.md"
    change.write_text("Make the web console usable on a phone.")

    report = _run_update(out, change, {"built": [], "brought_up": [], "preserve_data": [],
                                       "checked": []})
    assert report.status == "succeeded", report.status


def test_only_the_changed_service_is_rebuilt(tmp_path, updated):
    """The diff is mechanical, so an update on an adopted design must rebuild exactly the
    service whose description changed — the unchanged one reuses its adopted source."""
    out, _ = _adopt(tmp_path)
    change = tmp_path / "change.md"
    change.write_text("Rework the web console for phones.")

    report = _run_update(out, change, updated, change_service="web")
    assert report.status == "succeeded"
    assert updated["built"] == ["web"], "api was unchanged and must not rebuild"
    assert report.node_results["api"].detail == "unchanged: prior build reused"


def test_the_adopted_source_is_what_an_unchanged_service_reuses(tmp_path, updated):
    """`unchanged: prior build reused` is only true if adopt put the source where update-system
    looks for it — services/<name>/out."""
    out, _ = _adopt(tmp_path)
    assert (out / "services/api/out/pom.xml").is_file()
    change = tmp_path / "c.md"
    change.write_text("Change the web console only.")
    _run_update(out, change, updated, change_service="web")
    assert (out / "services/api/out/pom.xml").is_file(), "the reused tree must survive"


def test_the_update_rewrites_the_design_as_the_authoritative_record(tmp_path, updated):
    out, _ = _adopt(tmp_path)
    before = load_design(out)
    change = tmp_path / "c.md"
    change.write_text("Change the web console.")
    _run_update(out, change, updated, change_service="web")

    after = load_design(out)
    web_before = next(s for s in before.services if s.name == "web")
    web_after = next(s for s in after.services if s.name == "web")
    assert web_after.prd_slice.startswith("CHANGED: ")
    assert web_before.prd_slice != web_after.prd_slice


def test_an_adopted_design_with_no_db_schema_preserves_data(tmp_path, updated):
    """Data fate is the diff's schema_changed flag. An adoption with no db_schema contract must
    resolve to preserve — an update to an existing system must not silently wipe its datastore."""
    out, _ = _adopt(tmp_path)
    change = tmp_path / "c.md"
    change.write_text("Change the web console.")
    _run_update(out, change, updated, change_service="web")
    assert updated["preserve_data"] == [True]


def test_the_update_never_touches_the_source_repo(tmp_path, updated):
    """Adopt promises never to write to the source. An update driven off its artifacts must
    inherit that: it operates on the run dir's copy, not the operator's repository."""
    repo = tmp_path / "src-repo"
    out, _ = _adopt(tmp_path)
    before = {p.relative_to(repo): p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}

    change = tmp_path / "c.md"
    change.write_text("Change the web console.")
    _run_update(out, change, updated, change_service="web")

    after = {p.relative_to(repo): p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}
    assert before == after, "the update modified the adopted source repository"


def test_the_ledger_stays_append_only_across_both_lanes(tmp_path, updated):
    """One run dir, two lanes writing to it. The adopt trail must still be readable after the
    update appends — a truncating writer would destroy the provenance the review gate needs."""
    out, _ = _adopt(tmp_path)
    adopt_events = [json.loads(l)["event"]
                    for l in (out / "ledger.jsonl").read_text().splitlines()]
    change = tmp_path / "c.md"
    change.write_text("Change the web console.")
    _run_update(out, change, updated, change_service="web")

    all_events = [json.loads(l)["event"]
                  for l in (out / "ledger.jsonl").read_text().splitlines()]
    assert all_events[:len(adopt_events)] == adopt_events, "adopt's trail was rewritten"
    assert "system_update_start" in all_events
    assert "system_build_end" in all_events


def test_a_hand_edited_design_fails_on_load_not_mid_update(tmp_path, updated):
    """load_design re-runs the validators, so a corrupted artifact must fail before any build
    is attempted. The review gate depends on this: an unreviewed edit cannot slip through."""
    out, _ = _adopt(tmp_path)
    (out / "design.json").write_text('{"title": "broken", "services": [{"id": "x"}]}')
    change = tmp_path / "c.md"
    change.write_text("anything")
    with pytest.raises(Exception):
        _run_update(out, change, updated)
    assert updated["built"] == [], "nothing may be built from an invalid design"


# --------------------------------------------- what the hand-off does NOT carry

def _backend_only_repo(tmp_path):
    repo = tmp_path / "backend-only"
    (repo / "api/src/main/java").mkdir(parents=True)
    (repo / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>root</artifactId>'
        "<packaging>pom</packaging><modules><module>api</module></modules></project>")
    (repo / "api/pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>api</artifactId>'
        "<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>")
    (repo / "api/src/main/java/App.java").write_text("@SpringBootApplication class App {}")
    return repo


def test_an_unmapped_backend_only_adoption_has_no_integration_surface(tmp_path, updated):
    """The honest limit of an adoption with no map stage. With no contracts and no frontend there is
    nothing derivable to probe, and IntegrationReport.ok is False on zero steps by design — so
    the update reports integration_failed. That is correct, not a defect: the build succeeded
    and NOTHING about the running system was proved. The wrong fixes would be to invent a route
    (a Spring Boot service does not answer 200 at '/') or to let an empty report read as
    success. What the producer owes the operator is that this is stated at the human gate."""
    out = tmp_path / "adopted"
    result = adopt(_backend_only_repo(tmp_path), out,
                   prover=lambda d, rd, **kw: [Verdict(n.name, UNMAPPABLE, "stubbed")
                                               for n in d.services])
    assert result.status == "adopted", result.detail
    assert derive_integration_checks(load_design(out)) == []

    change = tmp_path / "c.md"
    change.write_text("Change the api.")
    report = _run_update(out, change, updated, change_service="api")
    assert report.status == "integration_failed"
    assert updated["built"] == ["api"], "the build half of the hand-off still worked"
    assert updated["checked"] == [[]], "nothing was invented to probe"


def test_a_mapped_adoption_carries_its_verification_surface(tmp_path, updated):
    """The complement: once a contract exists, the derived check set covers the producer, so an
    update on an adopted system verifies the same shapes the mapper recorded."""
    out, _ = _adopt(tmp_path)
    design = load_design(out)
    data = json.loads(design.model_dump_json())
    data["contracts"] = [{
        "id": "api-http", "kind": "openapi", "producer": "api",
        "spec": {"paths": {"/employees": {"get": {"responses": {"200": {"content": {
            "application/json": {"schema": {"type": "array", "items": {"type": "object",
                                            "properties": {"id": {"type": "integer"}}}}}}}}}}}},
    }]
    for svc in data["services"]:
        if svc["name"] == "api":
            svc["provides"] = ["api-http"]
        if svc["name"] == "web":
            svc["consumes"] = ["api-http"]
            svc["depends_on"] = ["api"]
    (out / "design.json").write_text(json.dumps(data, indent=2))

    checks = derive_integration_checks(load_design(out))
    assert ("api", "/employees") in [(c.service, c.route) for c in checks]

    change = tmp_path / "c.md"
    change.write_text("Change the web console.")
    report = _run_update(out, change, updated, change_service="web")
    assert report.status == "succeeded"
    assert ("api", "/employees") in updated["checked"][0]


def test_the_report_states_the_missing_surface_at_the_human_gate(tmp_path):
    """Adoption ends at a human review gate before anything trusts it, so the one place this
    limit must appear is the page that human reads — not in a later run's failure."""
    from devagent.adopt.report import write_report

    out = tmp_path / "adopted"
    result = adopt(_backend_only_repo(tmp_path), out,
                   prover=lambda d, rd, **kw: [Verdict(n.name, UNMAPPABLE, "stubbed")
                                               for n in d.services])
    page = write_report(result, out / "report.html").read_text()
    assert "No integration surface" in page
    assert "update-system" in page


def test_a_frontend_adoption_report_omits_the_missing_surface_caveat(tmp_path):
    """The caveat must track reality: a design with a probeable root does have a surface, and a
    caveat that always fires teaches the reviewer to ignore it."""
    from devagent.adopt.report import write_report

    out = tmp_path / "adopted"
    result = adopt(_fixture_repo(tmp_path), out,
                   prover=lambda d, rd, **kw: [Verdict(n.name, UNMAPPABLE, "stubbed")
                                               for n in d.services])
    page = write_report(result, out / "report.html").read_text()
    assert "No integration surface" not in page
