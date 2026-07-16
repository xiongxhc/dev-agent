"""M7 gitops: slug/forge-client unit tests (zero-network: urlopen is monkeypatched)."""
import io
import json
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from devagent.gitops import ForgeClient, GitPublisher, slugify
from devagent.tree import FAILED, SUCCEEDED, NodeResult


def test_slugify_normalizes_titles():
    assert slugify("Expense Tracker App") == "expense-tracker-app"
    assert slugify("  Notes/API v2!  ") == "notes-api-v2"
    assert slugify("!!!") == "app"
    assert len(slugify("x" * 200)) <= 60


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_create_project_posts_to_gitlab(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(req)
        return _Resp(json.dumps({
            "web_url": "https://gitlab.test/apps/notes-abc",
            "http_url_to_repo": "https://gitlab.test/apps/notes-abc.git",
            "path_with_namespace": "apps/notes-abc",
            "default_branch": "master",
        }).encode())

    import devagent.gitops as gitops
    monkeypatch.setattr(gitops.urllib.request, "urlopen", fake_urlopen)
    proj = ForgeClient("https://gitlab.test/", "sekrit", "42").create_project("notes-abc")

    assert proj["path_with_namespace"] == "apps/notes-abc"
    req = calls[0]           # numeric group: no group-lookup GET, straight to POST
    assert req.full_url == "https://gitlab.test/api/v4/projects"
    assert req.get_method() == "POST"
    assert req.headers["Private-token"] == "sekrit"
    body = dict(urllib.parse.parse_qsl(req.data.decode()))
    assert body == {"name": "notes-abc", "namespace_id": "42",
                    "visibility": "private", "initialize_with_readme": "false"}


def test_group_path_is_resolved_to_id(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(req)
        if "/groups/" in req.full_url:
            return _Resp(json.dumps({"id": 7}).encode())
        return _Resp(json.dumps({"web_url": "u", "http_url_to_repo": "r",
                                 "path_with_namespace": "p", "default_branch": "master"}).encode())

    import devagent.gitops as gitops
    monkeypatch.setattr(gitops.urllib.request, "urlopen", fake_urlopen)
    ForgeClient("https://gitlab.test", "t", "dev-agent/apps").create_project("n")

    assert calls[0].full_url == "https://gitlab.test/api/v4/groups/dev-agent%2Fapps"
    assert dict(urllib.parse.parse_qsl(calls[1].data.decode()))["namespace_id"] == "7"


class FakeForge:
    """create_project() backed by a local bare repo — pushes go over file://, no network."""

    def __init__(self, remotes_dir, fail=False):
        self.remotes_dir = Path(remotes_dir)
        self.created: list[str] = []
        self.fail = fail

    def create_project(self, name):
        if self.fail:
            raise RuntimeError("forge down")
        self.remotes_dir.mkdir(parents=True, exist_ok=True)
        bare = self.remotes_dir / f"{name}.git"
        subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)],
                       check=True, capture_output=True)
        self.created.append(name)
        return {"web_url": f"https://gitlab.test/apps/{name}",
                "http_url_to_repo": f"file://{bare}",
                "path_with_namespace": f"apps/{name}", "default_branch": "master"}


def _mkpub(tmp_path, **kw):
    run_dir = tmp_path / "run-1783944614-6ee72423"
    (run_dir / "services").mkdir(parents=True)
    forge = FakeForge(tmp_path / "remotes")
    ledger = []                      # Ledger's only used surface is .append(dict)
    return GitPublisher(run_dir, forge, ledger=ledger, **kw), run_dir, forge, ledger


def _remote_subjects(forge, name):
    bare = forge.remotes_dir / f"{name}.git"
    out = subprocess.run(["git", "-C", str(bare), "log", "--format=%s", "master"],
                         capture_output=True, text=True)
    return out.stdout.strip().splitlines()


def test_from_env_none_unless_fully_configured(tmp_path):
    env = {"DEVAGENT_GITLAB_URL": "https://g", "DEVAGENT_GITLAB_TOKEN": "t"}
    assert GitPublisher.from_env(tmp_path, env=env) is None      # group missing
    env["DEVAGENT_GITLAB_GROUP"] = "7"
    pub = GitPublisher.from_env(tmp_path, env=env)
    assert pub is not None and pub.forge.group == "7"


def test_ensure_repo_creates_once_and_persists_binding(tmp_path):
    pub, run_dir, forge, ledger = _mkpub(tmp_path)
    pub._ensure_repo("Expense Tracker")
    pub._ensure_repo("Expense Tracker")                          # idempotent
    assert forge.created == ["expense-tracker-6ee72423"]         # slug + run-id suffix
    binding = json.loads((run_dir / "repo.json").read_text())
    assert binding["url"] == "https://gitlab.test/apps/expense-tracker-6ee72423"
    assert binding["default_branch"] == "master"
    assert "sekrit" not in (run_dir / "repo.json").read_text()   # no token material
    assert (run_dir / "repo" / ".git").is_dir()
    assert {"event": "repo", "url": binding["url"]} in ledger


def test_ensure_repo_reuses_persisted_binding_and_reinits_clone(tmp_path):
    pub, run_dir, forge, _ = _mkpub(tmp_path)
    pub._ensure_repo("Notes")
    shutil.rmtree(run_dir / "repo")                              # clone lost (host moved)
    pub2 = GitPublisher(run_dir, forge)                          # fresh publisher, same run
    pub2._ensure_repo("Notes")
    assert forge.created == ["notes-6ee72423"]                   # no second project
    assert (run_dir / "repo" / ".git").is_dir()


def _node(name):
    return SimpleNamespace(id=name, name=name)


_DESIGN = SimpleNamespace(title="Expense Tracker")


def _green_out(run_dir, name, files=("app.py",)):
    out = run_dir / "services" / name / "out"
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        p = out / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {f}\n")
    return out


def _inner(status=SUCCEEDED):
    calls = []

    def run_node(node, design, repair_context=None):
        calls.append((node.name, repair_context))
        return NodeResult(node.id, status)

    return run_node, calls


def test_wrap_publishes_on_green(tmp_path):
    pub, run_dir, forge, _ = _mkpub(tmp_path)
    _green_out(run_dir, "api")
    inner, _ = _inner()
    nr = pub.wrap(inner)(_node("api"), _DESIGN)
    assert nr.status == SUCCEEDED                                # inner result untouched
    assert forge.created == ["expense-tracker-6ee72423"]         # lazy create on first green
    assert _remote_subjects(forge, "expense-tracker-6ee72423") == ["api: verified green"]
    clone = subprocess.run(["git", "-C", str(run_dir / "repo"), "show", "--stat", "HEAD"],
                           capture_output=True, text=True).stdout
    assert "services/api/app.py" in clone


def test_wrap_skips_failed_and_datastore_nodes(tmp_path):
    pub, run_dir, forge, _ = _mkpub(tmp_path)
    _green_out(run_dir, "api")
    failed, _ = _inner(status=FAILED)
    pub.wrap(failed)(_node("api"), _DESIGN)                      # FAILED -> no publish
    green, _ = _inner()
    pub.wrap(green)(_node("db"), _DESIGN)                        # green but no out/ -> skip
    assert forge.created == []


def test_wrap_excludes_junk_dirs(tmp_path):
    pub, run_dir, forge, _ = _mkpub(tmp_path)
    _green_out(run_dir, "api", files=("app.py", "node_modules/x/i.js", "__pycache__/a.pyc"))
    inner, _ = _inner()
    pub.wrap(inner)(_node("api"), _DESIGN)
    tree = subprocess.run(["git", "-C", str(run_dir / "repo"), "ls-files"],
                          capture_output=True, text=True).stdout
    assert "services/api/app.py" in tree
    assert "node_modules" not in tree and "__pycache__" not in tree


def test_wrap_repair_and_update_prefix_messages(tmp_path):
    pub, run_dir, forge, _ = _mkpub(tmp_path, commit_prefix="add a count endpoint")
    _green_out(run_dir, "api")
    inner, _ = _inner()
    pub.wrap(inner)(_node("api"), _DESIGN, repair_context="integration said so")
    subjects = _remote_subjects(forge, "expense-tracker-6ee72423")
    assert subjects == ['update "add a count endpoint": api: repaired']


def test_wrap_serializes_concurrent_sibling_publishes(tmp_path):
    # TreeOrchestrator runs sibling run_nodes concurrently (ThreadPoolExecutor, tree.py),
    # all sharing one GitPublisher/clone. Without a lock, concurrent copy->add->commit->push
    # sequences race on the working tree and .git/index.lock -> repo_error + dormancy.
    pub, run_dir, forge, ledger = _mkpub(tmp_path)
    names = [f"svc{i}" for i in range(4)]
    for name in names:
        _green_out(run_dir, name, files=(f"{name}.py",))
    inner, _ = _inner()
    wrapped = pub.wrap(inner)
    barrier = threading.Barrier(len(names))

    def publish(name):
        barrier.wait()                # line all 4 threads up before they hit the publisher
        wrapped(_node(name), _DESIGN)

    threads = [threading.Thread(target=publish, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not any(e.get("event") == "repo_error" for e in ledger)
    assert pub.dormant is False

    repo_name = "expense-tracker-6ee72423"
    subjects = _remote_subjects(forge, repo_name)
    assert set(subjects) == {f"{n}: verified green" for n in names}
    assert len(subjects) == 4

    bare = forge.remotes_dir / f"{repo_name}.git"
    log = subprocess.run(["git", "-C", str(bare), "log", "--format=%H", "master"],
                         capture_output=True, text=True).stdout.split()
    for sha in log:
        paths = subprocess.run(
            ["git", "-C", str(bare), "show", "--name-only", "--format=", sha],
            capture_output=True, text=True).stdout.split()
        prefixes = {p.split("/")[1] for p in paths if p.startswith("services/")}
        assert len(prefixes) == 1                # each commit touches one service only


def test_forge_failure_never_fails_green_build_and_goes_dormant(tmp_path):
    run_dir = tmp_path / "run-1-x"
    (run_dir / "services").mkdir(parents=True)
    ledger = []
    pub = GitPublisher(run_dir, FakeForge(tmp_path / "r", fail=True), ledger=ledger)
    _green_out(run_dir, "api")
    inner, calls = _inner()
    wrapped = pub.wrap(inner)

    nr = wrapped(_node("api"), _DESIGN)
    assert nr.status == SUCCEEDED                        # the build outcome is untouched
    assert [e["event"] for e in ledger] == ["repo_error"]
    assert pub.dormant

    _green_out(run_dir, "web")
    wrapped(_node("web"), _DESIGN)                       # dormant: no retry storm
    assert [e["event"] for e in ledger] == ["repo_error"]  # still exactly one
    assert len(calls) == 2                               # inner builder always runs


def test_token_never_lands_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVAGENT_GITLAB_TOKEN", "sekrit-token-xyz")
    pub, run_dir, forge, _ = _mkpub(tmp_path)
    _green_out(run_dir, "api")
    inner, _ = _inner()
    pub.wrap(inner)(_node("api"), _DESIGN)

    hits = [p for p in (run_dir / "repo").rglob("*")
            if p.is_file() and "sekrit-token-xyz" in p.read_text(errors="ignore")]
    assert hits == []
    assert "sekrit-token-xyz" not in (run_dir / "repo.json").read_text()
    # the helper is passed per-invocation via `-c` (never written to config), and it
    # reads the env var by NAME at push time — so no credential config may persist:
    assert "credential" not in (run_dir / "repo" / ".git" / "config").read_text()


def _report(title="Expense Tracker", urls=None):
    return SimpleNamespace(title=title, status="succeeded", urls=urls or {})


def test_finalize_syncs_readme_metadata_and_deletes_removed(tmp_path):
    pub, run_dir, forge, ledger = _mkpub(tmp_path)
    (run_dir / "design.json").write_text('{"title": "Expense Tracker"}')
    _green_out(run_dir, "api")
    _green_out(run_dir, "web")
    inner, _ = _inner()
    wrapped = pub.wrap(inner)
    wrapped(_node("api"), _DESIGN)
    wrapped(_node("web"), _DESIGN)

    shutil.rmtree(run_dir / "services" / "web")          # e.g. _reap_removed_services ran
    prd = tmp_path / "prd.md"
    prd.write_text("Build me an expense tracker.")
    pub.finalize(_report(urls={"api": "http://localhost:59001"}), prd_path=prd)

    tree = subprocess.run(["git", "-C", str(run_dir / "repo"), "ls-files"],
                          capture_output=True, text=True).stdout
    assert "services/api/app.py" in tree
    assert "services/web" not in tree                    # delete-sync removed it
    assert ".devagent/design.json" in tree and ".devagent/prd.md" in tree
    readme = (run_dir / "repo" / "README.md").read_text()
    assert "Expense Tracker" in readme and "http://localhost:59001" in readme
    assert {"event": "repo", "url": pub.binding["url"]} in ledger


def test_finalize_creates_repo_when_no_service_was_rebuilt(tmp_path):
    # An update that rebuilt zero services on a pre-M7 app: the wrapper never fired,
    # finalize alone must still create + publish (spec: finalize ensures the repo).
    pub, run_dir, forge, _ = _mkpub(tmp_path)
    _green_out(run_dir, "api")                           # prior build's out/ exists on disk
    pub.finalize(_report())
    assert forge.created == ["expense-tracker-6ee72423"]
    tree = subprocess.run(["git", "-C", str(run_dir / "repo"), "ls-files"],
                          capture_output=True, text=True).stdout
    assert "services/api/app.py" in tree and "README.md" in tree


def test_finalize_appends_change_history(tmp_path):
    pub, run_dir, forge, _ = _mkpub(tmp_path, commit_prefix="add a count endpoint")
    _green_out(run_dir, "api")
    pub.finalize(_report(), change_note="add a count endpoint")
    pub.finalize(_report(), change_note="rename count to total")
    changes = (run_dir / "repo" / ".devagent" / "change.md").read_text()
    assert changes == "- add a count endpoint\n- rename count to total\n"


def _seed_remote(tmp_path, with_develop=True):
    """A bare 'existing repo' with main (+ optional develop) history, like a user's repo."""
    bare = tmp_path / "existing.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)

    def g(*a):
        subprocess.run(["git", "-C", str(work), "-c", "user.name=t",
                        "-c", "user.email=t@t", *a], check=True, capture_output=True)

    (work / "README.md").write_text("theirs\n")
    g("add", "-A"); g("commit", "-m", "initial"); g("push", "origin", "main")
    if with_develop:
        g("checkout", "-b", "develop")
        (work / "dev.txt").write_text("d\n")
        g("add", "-A"); g("commit", "-m", "develop tip"); g("push", "origin", "develop")
    return bare


def _heads(bare):
    out = subprocess.run(["git", "-C", str(bare), "branch", "--format=%(refname:short)"],
                         capture_output=True, text=True)
    return set(out.stdout.split())


def test_existing_repo_branches_off_develop(tmp_path):
    bare = _seed_remote(tmp_path)
    run_dir = tmp_path / "run-1-6ee72423"
    (run_dir / "services").mkdir(parents=True)
    forge = FakeForge(tmp_path / "r")
    ledger = []
    pub = GitPublisher(run_dir, forge, ledger=ledger, repo_url=f"file://{bare}")
    _green_out(run_dir, "api")
    inner, _ = _inner()
    pub.wrap(inner)(_node("api"), _DESIGN)

    assert forge.created == []                                   # no new project
    branch = "devagent/expense-tracker-6ee72423"
    assert branch in _heads(bare)
    tree = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", branch],
                          capture_output=True, text=True).stdout
    assert "dev.txt" in tree                                     # based on develop
    assert "services/api/app.py" in tree
    develop = subprocess.run(["git", "-C", str(bare), "log", "--format=%s", "develop"],
                             capture_output=True, text=True).stdout.splitlines()
    assert develop == ["develop tip", "initial"]                 # their branches untouched
    web = f"file://{bare}"[:-4]                                  # binding strips ".git"
    assert ledger[0] == {"event": "repo", "url": f"{web}/-/tree/{branch}"}


def test_existing_repo_without_develop_uses_default_branch(tmp_path):
    bare = _seed_remote(tmp_path, with_develop=False)
    run_dir = tmp_path / "run-1-abc"
    (run_dir / "services").mkdir(parents=True)
    pub = GitPublisher(run_dir, FakeForge(tmp_path / "r"), repo_url=f"file://{bare}")
    _green_out(run_dir, "api")
    inner, _ = _inner()
    pub.wrap(inner)(_node("api"), _DESIGN)

    branch = "devagent/expense-tracker-abc"
    assert branch in _heads(bare)
    tree = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", branch],
                          capture_output=True, text=True).stdout
    assert "README.md" in tree and "services/api/app.py" in tree  # based on main
    assert json.loads((run_dir / "repo.json").read_text())["base"] == "main"


def test_publish_empty_artifact_does_not_kill_the_run(tmp_path):
    # First service produces zero tracked files (all-excluded): publish should not crash.
    # Then a real service: verify the remote has only the second commit (first was skipped).
    pub, run_dir, forge, ledger = _mkpub(tmp_path)
    api_out = run_dir / "services" / "api" / "out"
    api_out.mkdir(parents=True, exist_ok=True)
    (api_out / "node_modules" / "x.js").parent.mkdir(parents=True, exist_ok=True)
    (api_out / "node_modules" / "x.js").write_text("// excluded\n")  # all-excluded
    inner, _ = _inner()
    wrapped = pub.wrap(inner)
    nr = wrapped(_node("api"), _DESIGN)
    assert nr.status == SUCCEEDED
    assert pub.dormant is False
    assert not any(e.get("event") == "repo_error" for e in ledger)

    _green_out(run_dir, "web")
    wrapped(_node("web"), _DESIGN)
    subjects = _remote_subjects(forge, "expense-tracker-6ee72423")
    assert subjects == ["web: verified green"]  # only the second commit (first was empty)


def test_finalize_zero_services_writes_readme_only_snapshot(tmp_path):
    # All-datastore system: no services/ output at all. finalize must not crash.
    pub, run_dir, forge, ledger = _mkpub(tmp_path)
    pub.finalize(_report())
    assert pub.dormant is False
    assert not any(e.get("event") == "repo_error" for e in ledger)
    assert (run_dir / "repo" / "README.md").exists()
    readme = (run_dir / "repo" / "README.md").read_text()
    assert "- (none)" in readme  # services list should say none
    subjects = _remote_subjects(forge, "expense-tracker-6ee72423")
    assert len(subjects) == 1
    assert "README" in subjects[0] or "metadata" in subjects[0]


def test_rebase_url_reanchors_http_hosts_only():
    from devagent.gitops import rebase_url
    # live finding 2026-07-16: external_url host may not resolve from this machine
    assert (rebase_url("https://gitlab.internal.example/g/p.git", "https://192.0.2.10")
            == "https://192.0.2.10/g/p.git")
    assert rebase_url("https://gitlab.test/g/p", "https://gitlab.test") == "https://gitlab.test/g/p"
    assert rebase_url("file:///tmp/bare.git", "https://192.0.2.10") == "file:///tmp/bare.git"
    assert rebase_url("https://gitlab.test/g/p", None) == "https://gitlab.test/g/p"


def test_binding_rebases_forge_reported_urls(tmp_path):
    run_dir = tmp_path / "run-1-abc"
    (run_dir / "services").mkdir(parents=True)

    class ForeignHostForge:
        base_url = "https://10.0.0.9"

        def create_project(self, name):
            return {"web_url": f"https://gitlab.internal.example/apps/{name}",
                    "http_url_to_repo": f"https://gitlab.internal.example/apps/{name}.git",
                    "path_with_namespace": f"apps/{name}", "default_branch": "master"}

    ledger = []
    pub = GitPublisher(run_dir, ForeignHostForge(), ledger=ledger)
    pub._ensure_repo("Expense Tracker")      # binding built here; no push (host unreachable)

    binding = json.loads((run_dir / "repo.json").read_text())
    assert binding["remote"] == "https://10.0.0.9/apps/expense-tracker-abc.git"
    assert binding["url"] == "https://10.0.0.9/apps/expense-tracker-abc"
    assert ledger[0]["url"] == "https://10.0.0.9/apps/expense-tracker-abc"
