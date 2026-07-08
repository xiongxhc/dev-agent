"""Sandbox context for the phase pipeline.

Only `NullSandbox` remains. The brain phases (scope/plan/architect) run on the host and emit
artifacts without executing code, so they take a NullSandbox whose `.run()` raises if a phase
wrongly tries to exec. The build/verify phases do NOT use `ctx.sandbox` — `SdkExecutor` and
`BuildVerifier` each self-contain their own disposable, hardened Docker container (the
`devagent-sandbox:m2` image).

The original M1 `Sandbox` class (a standalone `--network none`, all-caps-dropped, read-only
container that execed a phase's commands) was the milestone-1 containment proof. When M2 moved
containment into the executor/verifier, nothing wired the `Sandbox` class into the build path,
so it has been retired along with the minimal `devagent-sandbox:m1` image."""


class SandboxError(RuntimeError):
    pass


class NullSandbox:
    """No-op sandbox for host-only phases (the brain phases — scope/plan/architect — call the
    LLM and emit artifacts; they never execute code). `.run()` raises so a host-only phase that
    wrongly tries to exec is caught. Build/verify phases self-contain their own Docker and never
    touch `ctx.sandbox`."""

    def __enter__(self) -> "NullSandbox":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def run(self, cmd):
        raise SandboxError("NullSandbox cannot run commands — this phase must not use the sandbox")
