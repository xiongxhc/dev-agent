"""ManagedExecutor — the A/B arm B. Builds the app on Claude Managed Agents (a hosted cloud
sandbox) instead of our own Docker, then pulls the result out and lands it in workdir/out for
the SHARED verify/acceptance to re-check. Same Executor seam as SdkExecutor, so the A/B is fair.

Output mechanism (de-risked live, 2026-06-24): artifacts must be written under
/mnt/session/outputs/ (anywhere else in the sandbox is ephemeral); they then surface via
`files.list(scope_id=session, betas=["managed-agents-2026-04-01"])` and download via
`files.download`. The model's `write` tool did NOT reliably target that dir in probes, so the
build prompt has the agent BASH-`tar` the project (minus node_modules) into one app.tar.gz —
a single download + extract preserves the directory tree. The managed-agents beta header is
passed explicitly on the Files calls (the SDK auto-sets it on the agents/sessions endpoints)."""

import io
import os
import tarfile
import time
from pathlib import Path

from .executor import BuildRequest, BuildResult

BETA = "managed-agents-2026-04-01"
DEFAULT_MODEL = os.getenv("DEVAGENT_MANAGED_MODEL", "claude-opus-4-8")
OUTPUTS = "/mnt/session/outputs"
TARBALL = "app.tar.gz"

SYSTEM = ("You are a senior web engineer who builds and ships production web apps autonomously "
          "in a sandbox, using the bash and file tools. You finish the whole task without asking "
          "questions.")

PROMPT = """\
Build a {stack} web app from this Spec and Plan, then package it for export.

SPEC (JSON):
{spec}

BUILD PLAN — ordered tasks, each owns specific files; implement every task:
{plan}

Instructions:
- Scaffold a Vite + React + Tailwind project in a working directory (e.g. /workspace/app):
  package.json (PIN exact dependency versions — no ^ or ~), vite/tailwind/postcss config,
  index.html, and src/. Implement every page and satisfy every acceptance check.
- Run `pnpm install` then `pnpm build`; fix errors and re-run until the build succeeds and a
  dist/ directory is produced.
- THEN export the result as a single tarball (this is REQUIRED — outputs elsewhere are lost):
    mkdir -p {outputs}
    tar czf {outputs}/{tarball} --exclude=node_modules --exclude=.git -C /workspace/app .
  The tarball MUST contain package.json, pnpm-lock.yaml, the config files, src/, and dist/.
- Verify it exists with `ls -la {outputs}/{tarball}`, then reply exactly: BUILD COMPLETE
"""


class ManagedExecutor:
    def __init__(self, client=None, model: str = DEFAULT_MODEL,
                 poll_attempts: int = 8, poll_delay: float = 5.0):
        self.client = client
        self.model = model
        self.poll_attempts = poll_attempts
        self.poll_delay = poll_delay

    def _get_client(self):
        if self.client is not None:
            return self.client
        from anthropic import Anthropic
        return Anthropic()

    def build(self, req: BuildRequest) -> BuildResult:
        out = Path(req.workdir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        # The SHARED acceptance runner reads out/.devagent/spec.json for the checks; SdkExecutor
        # writes it as part of its container setup, so the managed arm must too (else acceptance
        # crashes on a missing spec and the run fails even though the app is fine).
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        (dev / "spec.json").write_text(req.spec.model_dump_json())
        (dev / "plan.json").write_text(req.plan.model_dump_json())
        client = self._get_client()
        t0 = time.monotonic()
        session = None
        try:
            agent = client.beta.agents.create(
                name="devagent-build", model=self.model, system=SYSTEM,
                tools=[{"type": "agent_toolset_20260401"}])
            env = client.beta.environments.create(
                name="devagent-build-env",
                config={"type": "cloud", "networking": {"type": "unrestricted"}})
            session = client.beta.sessions.create(
                agent=agent.id, environment_id=env.id, title=f"build {req.run_id}")
            prompt = PROMPT.format(
                stack=req.spec.stack, spec=req.spec.model_dump_json(indent=2),
                plan=req.plan.model_dump_json(indent=2), outputs=OUTPUTS, tarball=TARBALL)
            self._drain(client, session.id, prompt)

            tar_bytes = self._fetch_tarball(client, session.id)
            wall = time.monotonic() - t0
            if tar_bytes is None:
                return BuildResult(repo_path=str(out), success=False, wall_clock_s=wall,
                                   error=f"{TARBALL} not produced under {OUTPUTS}")
            self._extract(tar_bytes, out)
            built = (out / "dist" / "index.html").is_file()
            return BuildResult(
                repo_path=str(out), success=built, wall_clock_s=wall,
                # NOTE: managed-agents token/cost accounting (incl. $/session-hr) is a TODO —
                # needed for the M5 A/B cost comparison, not for the build itself.
                error=None if built else "tarball produced no dist/index.html")
        finally:
            if session is not None:
                try:
                    client.beta.sessions.delete(session.id)
                except Exception:  # noqa: BLE001 — best-effort cleanup; never mask the result
                    pass

    def _drain(self, client, session_id: str, prompt: str) -> None:
        with client.beta.sessions.events.stream(session_id) as stream:
            client.beta.sessions.events.send(session_id, events=[{
                "type": "user.message", "content": [{"type": "text", "text": prompt}]}])
            for event in stream:
                if getattr(event, "type", None) == "session.status_idle":
                    break

    def _fetch_tarball(self, client, session_id: str):
        for _ in range(self.poll_attempts):
            data = client.beta.files.list(scope_id=session_id, betas=[BETA]).data
            tgt = next((f for f in data if (getattr(f, "filename", "") or "").endswith(TARBALL)), None)
            if tgt is not None:
                return client.beta.files.download(tgt.id, betas=[BETA]).read()
            time.sleep(self.poll_delay)
        return None

    @staticmethod
    def _extract(tar_bytes: bytes, out: Path) -> None:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
            tf.extractall(out, filter="data")  # 'data' filter blocks path-traversal/absolute paths
