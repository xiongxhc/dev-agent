import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def pytest_collection_modifyitems(config, items):
    """Auto-skip @pytest.mark.docker tests when no daemon is reachable — UNLESS
    DEVAGENT_REQUIRE_DOCKER=1, in which case the containment suite must FAIL not skip
    (so a Docker-less CI job can't report false-green on M1's headline guarantee)."""
    if docker_available():
        return
    import os
    if os.environ.get("DEVAGENT_REQUIRE_DOCKER") == "1":
        pytest.exit(
            "DEVAGENT_REQUIRE_DOCKER=1 but no Docker daemon — containment tests cannot run",
            returncode=1,
        )
    skip = pytest.mark.skip(reason="docker daemon not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """Build the minimal M1 image once for the docker-marked tests."""
    tag = "devagent-sandbox:m1"
    proc = subprocess.run(
        ["docker", "build", "-t", tag, str(ROOT / "sandbox")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"image build failed: {proc.stderr}"
    return tag
