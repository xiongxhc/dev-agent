"""M7 — publish built systems to a GitLab monorepo (spec:
docs/planning/dev-agent/specs/2026-07-16-dev-agent-m7-git-publish-design.md).

One built app = one private GitLab project, created lazily on the first green service.
Per-green-service commit+push during builds/repairs (accretion); finalize() adds README +
.devagent metadata and a full-tree sync as the deliverable snapshot. Strictly additive:
active only when the DEVAGENT_GITLAB_* env vars are set, and no git/forge failure ever
fails a green build — failures ledger `repo_error` and the publisher goes dormant for the
run. The token never touches disk: pushes inject it via an ephemeral credential helper."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from .tree import SUCCEEDED

_EXCLUDES = ("node_modules", ".git", "__pycache__", ".venv")  # executor-tar excludes
# `!shell` credential helper: git runs it at push time; the token stays in process env.
_CRED_HELPER = "!f() { echo username=oauth2; echo password=$DEVAGENT_GITLAB_TOKEN; }; f"


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60].strip("-") or "app"


class ForgeClient:
    """Minimal GitLab REST client (urllib — house convention, no new deps)."""

    def __init__(self, base_url: str, token: str, group: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.group = group

    def create_project(self, name: str) -> dict:
        data = urllib.parse.urlencode({
            "name": name, "namespace_id": self._group_id(),
            "visibility": "private", "initialize_with_readme": "false",
        }).encode()
        return self._request("POST", "/api/v4/projects", data)

    def _group_id(self) -> int:
        if str(self.group).isdigit():
            return int(self.group)
        enc = urllib.parse.quote(str(self.group), safe="")
        return int(self._request("GET", f"/api/v4/groups/{enc}")["id"])

    def _request(self, method: str, path: str, data: bytes | None = None) -> dict:
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"PRIVATE-TOKEN": self.token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())


class GitPublisher:
    """Owns everything git for one run dir. Callers use only from_env()/wrap()/finalize();
    wrap and finalize never raise (additive publishing — see _guarded)."""

    def __init__(self, run_dir, forge, ledger=None, commit_prefix: str | None = None):
        self.run_dir = Path(run_dir)
        self.repo_dir = self.run_dir / "repo"
        self.forge = forge
        self.ledger = ledger
        self.commit_prefix = commit_prefix
        self.dormant = False
        binding_file = self.run_dir / "repo.json"
        self.binding = (json.loads(binding_file.read_text())
                        if binding_file.is_file() else None)

    @classmethod
    def from_env(cls, run_dir, ledger=None, commit_prefix=None, env=None):
        env = os.environ if env is None else env
        url = env.get("DEVAGENT_GITLAB_URL")
        token = env.get("DEVAGENT_GITLAB_TOKEN")
        group = env.get("DEVAGENT_GITLAB_GROUP")
        if not (url and token and group):
            return None
        return cls(run_dir, ForgeClient(url, token, group),
                   ledger=ledger, commit_prefix=commit_prefix)

    def _ensure_repo(self, title: str) -> None:
        if self.binding is None:
            name = f"{slugify(title)}-{self.run_dir.name.rsplit('-', 1)[-1]}"
            proj = self.forge.create_project(name)
            self.binding = {"url": proj["web_url"],
                            "remote": proj["http_url_to_repo"],   # credential-free
                            "project_path": proj["path_with_namespace"],
                            "default_branch": proj.get("default_branch") or "master"}
            (self.run_dir / "repo.json").write_text(json.dumps(self.binding, indent=2))
            if self.ledger is not None:
                self.ledger.append({"event": "repo", "url": self.binding["url"]})
        if not (self.repo_dir / ".git").is_dir():
            # First creation, or the clone was lost: re-init. If the remote already has
            # history the next push is rejected (non-FF) -> repo_error; never force-push.
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "-b", self.binding["default_branch"])
            self._git("remote", "add", "origin", self.binding["remote"])

    def _git(self, *args: str) -> str:
        res = subprocess.run(
            ["git", "-c", f"credential.helper={_CRED_HELPER}",
             "-c", "user.name=dev-agent", "-c", "user.email=dev-agent@local", *args],
            cwd=self.repo_dir, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {res.stderr.strip()[:400]}")
        return res.stdout
