"""Disposable, hardened Docker sandbox. The blast-radius boundary for an agent that
self-approves commands. Per Anthropic's secure-deployment guidance (research C2/C3):
network-closed by default, all caps dropped, no-new-privileges, read-only rootfs,
non-root user, and the run's out/ dir as the ONLY writable host mount.

M2 will add an `allow_egress` param that swaps `--network none` for an out-of-sandbox
proxy allowlist. M1 starts fully closed — stronger, and the no-op phase needs no net.
"""

import os
import subprocess
from pathlib import Path

# Flags that cost nothing for a no-op but make the containment proof real.
HARDENED_FLAGS = [
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "256",
    "--memory", "2g",
    "--cpus", "2",
    "--read-only",
    "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
    "--network", "none",  # C3: closed by default
    "--ipc", "none",
]


class SandboxError(RuntimeError):
    pass


class Sandbox:
    """Context manager. Enter -> a detached --rm container; .run() execs inside it;
    exit -> the container is force-removed (proves disposability)."""

    def __init__(self, run_dir: Path, image: str, ledger=None, exec_timeout: int = 120):
        self.run_dir = Path(run_dir)
        # Absolute: Docker treats a relative -v source as a named volume, not a host dir.
        self.out_dir = (self.run_dir / "out").resolve()
        self.image = image
        self.ledger = ledger
        self.exec_timeout = exec_timeout
        self.cid: str | None = None

    def __enter__(self) -> "Sandbox":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # Run as the host user so the bind mount is writable without a world-writable
        # (0o777) dir; 0o700 suffices since container uid == host uid.
        self.out_dir.chmod(0o700)
        argv = [
            "docker", "run", "-d", "--rm",
            *HARDENED_FLAGS,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{self.out_dir}:/out",
            self.image, "sleep", "infinity",
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SandboxError(f"docker run failed: {proc.stderr.strip()}")
        self.cid = proc.stdout.strip()
        if self.ledger:
            # C2 auditability: record exactly how the box was provisioned.
            # NOTE (M2): never put secrets in argv — they would persist here in plaintext.
            self.ledger.append({
                "event": "sandbox_start",
                "container": self.cid[:12],
                "image": self.image,
                "image_digest": self._image_digest(),
                "argv": argv,
            })
        return self

    def run(self, cmd) -> tuple[int, str, str]:
        if self.cid is None:
            raise SandboxError("sandbox not started")
        if isinstance(cmd, str):
            cmd = ["sh", "-c", cmd]
        try:
            proc = subprocess.run(
                ["docker", "exec", self.cid, *cmd],
                capture_output=True, text=True, timeout=self.exec_timeout,
            )
        except subprocess.TimeoutExpired:
            # Surface as a failed command (non-zero exit) so the gate fails cleanly and
            # the orchestrator records a terminal status — never an uncaught crash.
            return 124, "", f"command timed out after {self.exec_timeout}s"
        return proc.returncode, proc.stdout, proc.stderr

    def __exit__(self, *exc) -> None:
        if self.cid:
            subprocess.run(["docker", "rm", "-f", self.cid],
                           capture_output=True, text=True)
            if self.ledger:
                self.ledger.append({"event": "sandbox_stop", "container": self.cid[:12]})
            self.cid = None

    def _image_digest(self) -> str:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", self.image],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return "unknown"  # provenance lookup failed — record it, don't fake a digest
        return proc.stdout.strip()
