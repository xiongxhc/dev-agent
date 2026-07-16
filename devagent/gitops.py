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
import threading
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

    def __init__(self, run_dir, forge, ledger=None, commit_prefix: str | None = None,
                repo_url: str | None = None):
        self.run_dir = Path(run_dir)
        self.repo_dir = self.run_dir / "repo"
        self.forge = forge
        self.ledger = ledger
        self.commit_prefix = commit_prefix
        self.repo_url = repo_url.strip() if repo_url else None
        self.dormant = False
        # TreeOrchestrator runs sibling run_nodes concurrently in a thread pool (tree.py);
        # all of them publish through this one publisher onto one shared clone, so the
        # copy->add->commit->push sequence must be serialized per publisher.
        self._lock = threading.Lock()
        binding_file = self.run_dir / "repo.json"
        self.binding = (json.loads(binding_file.read_text())
                        if binding_file.is_file() else None)

    @classmethod
    def from_env(cls, run_dir, ledger=None, commit_prefix=None, env=None, repo_url=None):
        env = os.environ if env is None else env
        url = env.get("DEVAGENT_GITLAB_URL")
        token = env.get("DEVAGENT_GITLAB_TOKEN")
        group = env.get("DEVAGENT_GITLAB_GROUP")
        if not (url and token and group):
            return None
        return cls(run_dir, ForgeClient(url, token, group),
                   ledger=ledger, commit_prefix=commit_prefix, repo_url=repo_url)

    def _ensure_repo(self, title: str) -> None:
        if self.binding is None:
            if self.repo_url:
                self._bind_existing(title)
                return                      # clone already materialized the worktree
            name = f"{slugify(title)}-{self.run_dir.name.rsplit('-', 1)[-1]}"
            proj = self.forge.create_project(name)
            self.binding = {"url": proj["web_url"],
                            "remote": proj["http_url_to_repo"],   # credential-free
                            "project_path": proj["path_with_namespace"],
                            "mode": "new",
                            "default_branch": proj.get("default_branch") or "master"}
            (self.run_dir / "repo.json").write_text(json.dumps(self.binding, indent=2))
            self._announce()
        if not (self.repo_dir / ".git").is_dir():
            if self.binding.get("mode") == "existing":
                # clone was lost: re-clone our branch (exists remotely after first push)
                self._git("clone", "--branch", self.binding["default_branch"],
                          self.binding["remote"], str(self.repo_dir), cwd=self.run_dir)
            else:
                # First creation, or the clone was lost: re-init. If the remote already
                # has history the next push is rejected (non-FF) -> repo_error; never
                # force-push.
                self.repo_dir.mkdir(parents=True, exist_ok=True)
                self._git("init", "-b", self.binding["default_branch"])
                self._git("remote", "add", "origin", self.binding["remote"])

    def _bind_existing(self, title: str) -> None:
        """User-supplied repo: publish on a fresh branch off develop (fallback: default
        branch); the user's branches are never pushed to. Empty repo (no base commit) ->
        checkout fails -> repo_error via _guarded (use new-repo mode for empty repos)."""
        self._git("clone", self.repo_url, str(self.repo_dir), cwd=self.run_dir)
        has_develop = self._git("ls-remote", "--heads", "origin", "develop").strip()
        base = "develop" if has_develop else self._default_branch()
        branch = f"devagent/{slugify(title)}-{self.run_dir.name.rsplit('-', 1)[-1]}"
        self._git("checkout", "-b", branch, f"origin/{base}")
        web = self.repo_url[:-4] if self.repo_url.endswith(".git") else self.repo_url
        # default_branch is "the branch pushes go to" throughout this class
        self.binding = {"url": web, "remote": self.repo_url, "mode": "existing",
                        "base": base, "default_branch": branch}
        (self.run_dir / "repo.json").write_text(json.dumps(self.binding, indent=2))
        self._announce()

    def _default_branch(self) -> str:
        out = self._git("symbolic-ref", "refs/remotes/origin/HEAD")  # set by clone
        return out.strip().rsplit("/", 1)[-1]

    def _announce(self) -> None:
        if self.ledger is None:
            return
        url = self.binding["url"]
        if self.binding.get("mode") == "existing":
            url = f"{url}/-/tree/{self.binding['default_branch']}"  # link lands on our branch
        self.ledger.append({"event": "repo", "url": url})

    def _git(self, *args: str, cwd=None) -> str:
        res = subprocess.run(
            ["git", "-c", f"credential.helper={_CRED_HELPER}",
             "-c", "user.name=dev-agent", "-c", "user.email=dev-agent@local", *args],
            cwd=cwd or self.repo_dir, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {res.stderr.strip()[:400]}")
        return res.stdout

    # -- run_node seam -----------------------------------------------------
    def wrap(self, run_node):
        """A run_node that publishes the service after the inner builder returns SUCCEEDED.
        In update-system, unchanged-node skips return before the inner run_node
        (system_build.update_run_node), so they can never re-commit here."""

        def wrapped(node, design, repair_context=None):
            nr = run_node(node, design, repair_context=repair_context)
            if getattr(nr, "status", None) == SUCCEEDED:
                self._guarded(self._publish_service, node, design,
                              repaired=repair_context is not None)
            return nr

        return wrapped

    def _guarded(self, fn, *a, **kw):
        with self._lock:
            if self.dormant:
                return
            try:
                fn(*a, **kw)
            except Exception as e:  # additive publishing: never fail a green build
                self.dormant = True
                if self.ledger is not None:
                    self.ledger.append({"event": "repo_error", "detail": repr(e)})

    def _publish_service(self, node, design, repaired: bool) -> None:
        out = self.run_dir / "services" / node.name / "out"
        if not out.is_dir():
            return  # datastore-style node: no buildable artifact (make_run_node docstring)
        self._ensure_repo(getattr(design, "title", None) or "app")
        self._copy_service(node.name, out)
        what = f"{node.name}: repaired" if repaired else f"{node.name}: verified green"
        self._commit_push(what)

    def _copy_service(self, name: str, out) -> None:
        dst = self.repo_dir / "services" / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(out, dst, ignore=shutil.ignore_patterns(*_EXCLUDES))

    def _commit_push(self, message: str) -> None:
        if self.commit_prefix:
            message = f'update "{self.commit_prefix}": {message}'
        self._git("add", "-A")
        if self._git("status", "--porcelain").strip():
            self._git("commit", "-m", message)
        try:
            self._git("rev-parse", "--verify", "-q", "HEAD")
        except RuntimeError:
            return  # nothing ever committed (empty first artifact): nothing to push
        self._git("push", "-u", "origin", self.binding["default_branch"])

    # -- deliverable snapshot ------------------------------------------------
    def finalize(self, report, prd_path=None, change_note=None) -> None:
        """Full-tree sync + README + .devagent metadata. Call only on a succeeded run."""
        self._guarded(self._finalize, report, prd_path, change_note)

    def _finalize(self, report, prd_path, change_note) -> None:
        # Ensure-first: an update that rebuilt zero services on a pre-M7 app never
        # triggered the wrapper, so the repo may not exist yet.
        self._ensure_repo(getattr(report, "title", None) or "app")
        services_root = self.repo_dir / "services"
        if services_root.exists():        # sync WITH DELETE: self-heals renames/reaps
            shutil.rmtree(services_root)
        for out in sorted((self.run_dir / "services").glob("*/out")):
            self._copy_service(out.parent.name, out)
        meta = self.repo_dir / ".devagent"
        meta.mkdir(exist_ok=True)
        design = self.run_dir / "design.json"
        if design.is_file():
            shutil.copy(design, meta / "design.json")
        if prd_path is not None and Path(prd_path).is_file():
            shutil.copy(prd_path, meta / "prd.md")
        if change_note:
            with (meta / "change.md").open("a", encoding="utf-8") as f:
                f.write(f"- {change_note}\n")
        (self.repo_dir / "README.md").write_text(self._readme(report), encoding="utf-8")
        self._commit_push("publish: README + .devagent metadata")
        self._announce()              # re-announce so an update's tail also sees the URL

    def _readme(self, report) -> str:
        urls = getattr(report, "urls", None) or {}
        previews = ("\n".join(f"- **{sid}**: {url}" for sid, url in sorted(urls.items()))
                    or "- (no live preview)")
        services_dir = self.repo_dir / "services"
        if services_dir.is_dir():
            services = "\n".join(
                f"- `services/{p.name}/`"
                for p in sorted(services_dir.iterdir()) if p.is_dir())
        else:
            services = "- (none)"
        return (f"# {getattr(report, 'title', 'app')}\n\n"
                "Built autonomously by dev-agent. Each service lives under "
                "`services/<name>/`; the gated design (incl. contracts) is "
                "`.devagent/design.json`, the original request `.devagent/prd.md`.\n\n"
                "> **One-way repo:** dev-agent publishes here but never reads back. "
                "Commits pushed directly to `services/` will be replaced by the next "
                "chat-driven update. Request changes in the app's chat instead.\n\n"
                f"## Services\n{services}\n\n## Preview (dev-agent host)\n{previews}\n")
