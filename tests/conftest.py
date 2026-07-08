import shutil
import subprocess

import pytest


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def pytest_collection_modifyitems(config, items):
    """Auto-skip @pytest.mark.docker tests when no daemon is reachable — UNLESS
    DEVAGENT_REQUIRE_DOCKER=1, in which case the containment suite must FAIL not skip
    (so a Docker-less CI job can't report false-green on the containment guarantee)."""
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


