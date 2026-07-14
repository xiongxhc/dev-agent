"""SdkExecutor — the self-built A/B arm. Launches the disposable M2 container and runs
the Claude Agent SDK INSIDE it (via sdk_runner.py) to build the app per the frozen
Scope+Plan, writing to the host-mounted /out. Returns a BuildResult.

The API key is passed by NAME (`-e ANTHROPIC_API_KEY`) so the value flows via the host
process env, never into the `docker run` argv (which the ledger records) — per the M1
security review. When an egress network is provided (the default for `--build`), the
container runs on the `--internal` allowlist network behind the proxy (see egress.py);
otherwise it uses the default bridge.

M12 — parallel build: a scope with more than one `kind=="build"` target runs **one contained
SDK session per build-target concurrently** (each its own container + install + build, all
mounting the shared /out — writes are disjoint by target dir, guaranteed by the plan's
pairwise-disjoint file ownership). Results aggregate into one BuildResult (tokens/cost summed,
wall-clock = max). A single-target scope takes the original sequential path unchanged. The
full enriched scope is always written to .devagent/scope.json so verify/acceptance (which run
the whole project) are untouched — parallelism is internal to the Executor seam.
"""

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import egress, recipes
from .executor import BuildRequest, BuildResult, broadcast_consumed, enrich_scope
from .schema import Plan, ProjectScope

RUNNER = Path(__file__).parent / "sdk_runner.py"
DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
DEFAULT_MAX_CONCURRENCY = 3   # host pressure: each target = an m2 container (+ chromium)


class SdkExecutor:
    def __init__(self, image: str = DEFAULT_IMAGE, runner: Path = RUNNER,
                 max_turns: int = 40, timeout: int = 1200,
                 api_key_env: str = "ANTHROPIC_API_KEY",
                 network: str | None = None, proxy_url: str | None = None,
                 model: str | None = None):
        self.image = image
        self.runner = Path(runner)
        self.max_turns = max_turns
        self.timeout = timeout
        self.api_key_env = api_key_env
        self.network = network        # egress-allowlist network (None = default bridge)
        self.proxy_url = proxy_url
        self.model = model            # build model for the in-container Agent SDK (None = SDK default)

    def build(self, req: BuildRequest) -> BuildResult:
        out = Path(req.workdir).resolve()
        dev = out / ".devagent"
        dev.mkdir(parents=True, exist_ok=True)
        # Always write the FULL enriched scope+plan — verify/acceptance run the whole project.
        (dev / "scope.json").write_text(
            json.dumps(enrich_scope(req.scope, broadcast_consumed(req.scope, req.consumed_contracts),
                                    broadcast_consumed(req.scope, req.provided_contracts))))
        (dev / "plan.json").write_text(req.plan.model_dump_json())
        repair, update = dev / "repair.txt", dev / "update.txt"
        repair.unlink(missing_ok=True)   # stale context from a prior pass must not leak in
        update.unlink(missing_ok=True)
        if req.repair_context:
            # The edit-in-place context: verify diagnostics (repair) or the user's change
            # request (M25 update). The runner picks the prompt framing by filename.
            (update if req.context_kind == "update" else repair).write_text(req.repair_context)

        build_targets = [t for t in req.scope.targets
                         if recipes.get(t.stack).kind == "build"]
        slices = self._partition(req.plan, build_targets)

        # Parallelize ONLY a fresh build whose plan cleanly partitions across the build targets
        # (>1 target, each owning ≥1 task, every task in exactly one target). A repair pass, a
        # single/zero build-target, or any non-partitionable plan takes the original sequential
        # session: one agent sees the whole tree, so there is no shared-/out collision and repair
        # stays whole-project. This is what guarantees the "writes are disjoint by target dir"
        # invariant the parallel path relies on, rather than assuming it.
        if slices is None or req.repair_context is not None:
            results = {"_single": self._run_one(out, target=None)}
        else:
            for bt in build_targets:
                bdir = dev / "build" / bt.name
                bdir.mkdir(parents=True, exist_ok=True)
                (bdir / "result.json").unlink(missing_ok=True)   # never read a prior run's result
                sub = ProjectScope(title=req.scope.title, targets=[bt])
                (bdir / "scope.json").write_text(
                    json.dumps(enrich_scope(sub, broadcast_consumed(sub, req.consumed_contracts),
                                            broadcast_consumed(sub, req.provided_contracts))))
                (bdir / "plan.json").write_text(Plan(tasks=slices[bt.name]).model_dump_json())
            cap = self._concurrency(len(build_targets))
            with ThreadPoolExecutor(max_workers=cap) as ex:
                futs = {ex.submit(self._run_one, out, bt.name): bt.name for bt in build_targets}
                done = {futs[f]: f.result() for f in as_completed(futs)}
            # Re-key in target order (not completion order) so aggregation is deterministic.
            results = {bt.name: done[bt.name] for bt in build_targets}

        return self._aggregate(out, req, results)

    # -- internals -----------------------------------------------------------

    def _concurrency(self, n_targets: int) -> int:
        env = os.getenv("DEVAGENT_BUILD_CONCURRENCY")
        cap = int(env) if env else DEFAULT_MAX_CONCURRENCY
        return max(1, min(n_targets, cap))

    @staticmethod
    def _partition(plan: Plan, build_targets: list) -> dict[str, list] | None:
        """Map each build target to the plan tasks it owns (a task is owned by a target when an
        owned_file sits under that target's dir). Returns None — meaning "not safely parallelizable,
        build sequentially" — unless the partition is CLEAN: more than one target, every target owns
        at least one task, and every task belongs to exactly one target (none unassigned, none
        spanning two). A clean partition is the only case where concurrent containers writing the
        shared /out cannot collide."""
        if len(build_targets) < 2:
            return None
        slices: dict[str, list] = {bt.name: [] for bt in build_targets}
        for t in plan.tasks:
            owners = [bt.name for bt in build_targets
                      if any(f == bt.name or f.startswith(bt.name + "/") for f in t.owned_files)]
            if len(owners) != 1:
                return None            # unassigned (root/shared) or cross-target task → not clean
            slices[owners[0]].append(t)
        if not all(slices[bt.name] for bt in build_targets):
            return None                # a target with no tasks → not a real partition
        return slices

    def _docker_argv(self, out: Path, target: str | None) -> list[str]:
        argv = [
            "docker", "run", "--rm",
            *egress.docker_flags(self.network, self.proxy_url),  # [] when egress disabled
            "-e", self.api_key_env,        # value via env, NOT argv (no secret in argv)
            "-e", "HOME=/home/node",
            # build model (not a secret): inline so the run is reproducible from the ledger argv.
            *(["-e", f"DEVAGENT_BUILD_MODEL={self.model}"] if self.model else []),
            "--user", "1000:1000",
            "-v", f"{out}:/out",
            "-v", f"{self.runner}:/runner.py:ro",
            self.image,
            "python3", "/runner.py", "--max-turns", str(self.max_turns),
        ]
        if target is not None:
            argv += ["--target", target]
        return argv

    def _run_one(self, out: Path, target: str | None) -> dict:
        """Run one build container (whole scope if target is None, else just that target).
        Returns the raw result dict augmented with _wall (this session's wall-clock) and
        _timeout/_result_path markers."""
        argv = self._docker_argv(out, target)
        t0 = time.monotonic()
        try:
            subprocess.run(argv, capture_output=True, text=True,
                           timeout=self.timeout, env={**os.environ})
        except subprocess.TimeoutExpired:
            return {"_timeout": True, "_wall": time.monotonic() - t0}
        wall = time.monotonic() - t0
        rp = (out / ".devagent" / "build" / target / "result.json") if target \
            else (out / ".devagent" / "result.json")
        r = json.loads(rp.read_text()) if rp.exists() else {}
        r["_wall"] = wall
        r["_result_path"] = str(rp) if rp.exists() else None
        return r

    def _aggregate(self, out: Path, req: BuildRequest, results: dict[str, dict]) -> BuildResult:
        raws = list(results.values())
        timed_out = next((r for r in raws if r.get("_timeout")), None)

        built = all(
            list((out / t.name).glob(recipes.get(t.stack).artifact_glob))
            for t in req.scope.targets
            if recipes.get(t.stack).kind == "build"
        )
        ok_stream = all(bool(r.get("ok_stream")) for r in raws) and timed_out is None

        costs = [r.get("cost_usd") for r in raws if r.get("cost_usd") is not None]
        errors = [r["error"] for r in raws if r.get("error")]
        if timed_out is not None:
            error = f"build timed out after {self.timeout}s"
        elif errors:
            error = errors[0]
        elif not built:
            error = "no target artifacts produced"
        else:
            error = None
        # transcript: the single session's result, or the first per-target result available.
        transcript = next((r.get("_result_path") for r in raws if r.get("_result_path")), None)

        return BuildResult(
            repo_path=str(out),
            success=ok_stream and built,  # CLAIM — the build gate re-checks
            tokens_in=sum(int(r.get("tokens_in") or 0) for r in raws),
            tokens_out=sum(int(r.get("tokens_out") or 0) for r in raws),
            cache_read_tokens=sum(int((r.get("usage") or {}).get("cache_read_input_tokens") or 0)
                                  for r in raws),
            wall_clock_s=max((r.get("_wall") or 0.0) for r in raws) if raws else 0.0,
            cost_usd=sum(costs) if costs else None,
            transcript_path=transcript,
            error=error,
        )
