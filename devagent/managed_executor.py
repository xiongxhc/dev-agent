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
import json
import os
import tarfile
import time
from pathlib import Path

from . import recipes
from .executor import BuildRequest, BuildResult, enrich_scope
from .sdk_runner import build_prompt, input_output_tokens

BETA = "managed-agents-2026-04-01"
DEFAULT_MODEL = os.getenv("DEVAGENT_MANAGED_MODEL", "claude-opus-4-8")
OUTPUTS = "/mnt/session/outputs"
TARBALL = "app.tar.gz"
# Managed Agents bills token rates PLUS a sandbox session-hour charge — the cost the SDK arm
# does NOT have, and the whole reason the A/B cost comparison exists. Override per Anthropic's rate.
SESSION_HR_USD = float(os.getenv("DEVAGENT_MANAGED_SESSION_HR_USD", "0.08"))

SYSTEM = ("You are a senior web engineer who builds and ships production web apps autonomously "
          "in a sandbox, using the bash and file tools. You finish the whole task without asking "
          "questions.")

TARBALL_INSTRUCTION = """\

OUTPUTS (REQUIRED — artifacts elsewhere are lost):
    mkdir -p {outputs}
    tar czf {outputs}/{tarball} --exclude=node_modules --exclude=.git -C /out .
The tarball MUST contain all target directories with their built artifacts.
Verify it exists with `ls -la {outputs}/{tarball}`, then reply exactly: BUILD COMPLETE
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
        # The SHARED acceptance runner reads out/.devagent/scope.json for the checks; SdkExecutor
        # writes it as part of its container setup, so the managed arm must too (else acceptance
        # crashes on a missing scope and the run fails even though the app is fine).
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        enriched = enrich_scope(req.scope)
        (dev / "scope.json").write_text(json.dumps(enriched))
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
            prompt = build_prompt(enriched, json.loads(req.plan.model_dump_json()))
            prompt += TARBALL_INSTRUCTION.format(outputs=OUTPUTS, tarball=TARBALL)
            spent = self._drain(client, session.id, prompt)

            tar_bytes = self._fetch_tarball(client, session.id)
            wall = time.monotonic() - t0
            cost = self._cost_fields(spent, wall)   # tokens + session-hr cost, for the A/B comparison
            if tar_bytes is None:
                return BuildResult(repo_path=str(out), success=False, wall_clock_s=wall, **cost,
                                   error=f"{TARBALL} not produced under {OUTPUTS}")
            self._extract(tar_bytes, out)
            built = all(
                list((out / t.name).glob(recipes.get(t.stack).artifact_glob))
                for t in req.scope.targets
            )
            return BuildResult(
                repo_path=str(out), success=built, wall_clock_s=wall, **cost,
                error=None if built else "no target artifacts produced")
        finally:
            if session is not None:
                try:
                    client.beta.sessions.delete(session.id)
                except Exception:  # noqa: BLE001 — best-effort cleanup; never mask the result
                    pass

    def _drain(self, client, session_id: str, prompt: str) -> dict:
        """Stream until the session goes idle, accumulating the cost signals the events carry.
        Defensive on field names (the managed-agents event schema isn't frozen): captures the
        last `usage` seen (treated as cumulative, like the SDK's terminal ResultMessage) and any
        reported cost_usd. Returns {usage, cost_usd}; the session-hr charge is added in build()."""
        usage: dict = {}
        cost_usd = None
        with client.beta.sessions.events.stream(session_id) as stream:
            client.beta.sessions.events.send(session_id, events=[{
                "type": "user.message", "content": [{"type": "text", "text": prompt}]}])
            for event in stream:
                u = getattr(event, "usage", None)
                if u is not None:
                    usage = u if isinstance(u, dict) else (getattr(u, "__dict__", None) or usage)
                c = getattr(event, "total_cost_usd", None)
                if c is None:
                    c = getattr(event, "cost_usd", None)
                if c is not None:
                    cost_usd = c
                if getattr(event, "type", None) == "session.status_idle":
                    break
        return {"usage": usage, "cost_usd": cost_usd}

    @staticmethod
    def _cost_fields(spent: dict, wall: float) -> dict:
        """BuildResult cost fields from drained usage + the wall-clock session-hour charge.
        cost_usd = (token cost the API reported, if any) + wall-hours * SESSION_HR_USD."""
        usage = spent.get("usage") or {}
        if not isinstance(usage, dict):
            usage = getattr(usage, "__dict__", None) or {}
        tin, tout = input_output_tokens(usage)
        session_cost = wall / 3600.0 * SESSION_HR_USD
        return {
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cost_usd": round((spent.get("cost_usd") or 0.0) + session_cost, 6),
        }

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
