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
