"""Host lock — one run at a time inside a section that inspects and replaces shared Docker state."""

import json
import os
import stat
import tempfile
import threading
import time

import pytest

from devagent import hostlock

LOCK_ROOT = hostlock.lock_root


@pytest.fixture(autouse=True)
def _lock_root(tmp_path, monkeypatch):
    monkeypatch.setattr(hostlock, "lock_root", lambda: tmp_path / "locks")


def test_a_lock_is_exclusive_and_releases(tmp_path):
    lock = tmp_path / "locks" / "egress.lock"
    with hostlock.exclusive("egress"):
        assert lock.exists()
        with pytest.raises(TimeoutError) as e:
            with hostlock.exclusive("egress", timeout_s=0.2, poll_s=0.05):
                pass
        assert "held by another run" in str(e.value)
    assert not lock.exists()


def test_a_second_holder_waits_until_the_first_releases(tmp_path):
    order, waited = [], []
    entered = threading.Event()

    def first():
        with hostlock.exclusive("egress"):
            entered.set()
            order.append("first in")
            time.sleep(0.4)
            order.append("first out")

    t = threading.Thread(target=first)
    t.start()
    entered.wait(2)
    with hostlock.exclusive("egress", timeout_s=5, poll_s=0.05, on_wait=waited.append):
        order.append("second in")
    t.join()
    assert order == ["first in", "first out", "second in"]
    assert waited == ["egress"], "waiting is announced once"


def test_a_crashed_holder_does_not_wedge_the_host(tmp_path):
    d = tmp_path / "locks"
    d.mkdir(parents=True)
    (d / "egress.lock").write_text(json.dumps({"pid": 999_999_999, "at": time.time()}))
    with hostlock.exclusive("egress", timeout_s=0.2):
        pass        # reclaimed by pid; no TimeoutError


def test_an_unparseable_lock_is_treated_as_stale(tmp_path):
    d = tmp_path / "locks"
    d.mkdir(parents=True)
    (d / "egress.lock").write_text("{ not json")
    with hostlock.exclusive("egress", timeout_s=0.2):
        pass


def test_an_uncontended_acquire_reads_no_clock(monkeypatch):
    reads = []
    real = time.monotonic
    monkeypatch.setattr(hostlock.time, "monotonic", lambda: (reads.append(1), real())[1])
    with hostlock.exclusive("quiet"):
        pass
    assert reads == []


def test_the_lock_root_depends_on_neither_the_runs_dir_nor_the_working_directory(
        tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    expected = tmp_path / "tmp" / f"devagent-{os.getuid()}" / "locks"
    seen = set()
    for runs, cwd in (("runs-a", "a"), ("runs-b", "b")):
        (tmp_path / cwd).mkdir()
        monkeypatch.setenv("DEVAGENT_RUNS_DIR", str(tmp_path / runs))
        monkeypatch.chdir(tmp_path / cwd)
        seen.add(LOCK_ROOT())
    assert seen == {expected}


def test_the_lock_directory_is_private_to_its_user(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(hostlock, "lock_root", LOCK_ROOT)
    user_dir = tmp_path / "tmp" / f"devagent-{os.getuid()}"
    with hostlock.exclusive("egress"):
        assert (user_dir / "locks" / "egress.lock").is_file()
    for d in (user_dir, user_dir / "locks"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
