"""M7 gitops: slug/forge-client unit tests (zero-network: urlopen is monkeypatched)."""
import io
import json
import urllib.parse

from devagent.gitops import ForgeClient, slugify


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
