"""Host-wide coordination between concurrent dev-agent runs.

One run on a laptop needs none of this. Several do: `egress.ensure()` is an inspect-then-act on a
SHARED proxy container. Two runs starting together both see "no proxy" and both create one, and a run
needing a different allowlist can replace the proxy while another run is deciding the same thing.

The primitive is a file in the runs root, because that is the one place every run on a host already
agrees on, it survives a crash (a stale lock is detectable by age and by the pid inside it), and it
needs no daemon.

Deliberately NOT distributed. This coordinates runs on ONE host.
"""

import errno
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = ".locks"
# How long before a lock is presumed abandoned. Generous: stealing a live lock is worse than waiting.
STALE_AFTER_S = 3 * 3600


def _dir(runs_root) -> Path:
    d = Path(runs_root) / LOCK_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _alive(pid: int) -> bool:
    """Is this pid still running? Absence is how a crashed holder releases its lock."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM      # exists but owned by someone else


def _stale(p: Path) -> bool:
    try:
        body = json.loads(p.read_text())
        if time.time() - float(body.get("at", 0)) > STALE_AFTER_S:
            return True
        return not _alive(int(body.get("pid", 0)))
    except (OSError, ValueError):
        return True        # unreadable is stale; a lock nobody can parse cannot be respected


@contextmanager
def exclusive(runs_root, name: str, *, timeout_s: float = 120.0, poll_s: float = 0.25,
              on_wait=None):
    """Hold `name` exclusively for the duration of the block.

    O_EXCL creation is the primitive, atomic on every filesystem this runs on. A holder that crashed
    is detected by its pid being gone rather than by a timeout alone, so a crash costs the next run a
    quarter of a second and not two minutes.

    On timeout it RAISES. Silently proceeding without the lock would defeat the point, and silently
    waiting forever is how a queue turns into a hang.
    """
    path = _dir(runs_root) / f"{name}.lock"
    # The deadline is computed lazily, on the first failed attempt, so an uncontended acquire reads
    # no clock at all.
    deadline = None
    waited = False
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time(),
                                     "name": name}).encode())
            os.close(fd)
            break
        except FileExistsError:
            if _stale(path):
                path.unlink(missing_ok=True)
                continue
            if deadline is None:
                deadline = time.monotonic() + timeout_s
            elif time.monotonic() > deadline:
                raise TimeoutError(
                    f"waited {timeout_s:.0f}s for the {name!r} lock held by another run "
                    f"({path}). If no run is active, delete it.")
            if not waited and on_wait is not None:
                on_wait(name)          # said once, because a waiting run must not look hung
                waited = True
            time.sleep(poll_s)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
