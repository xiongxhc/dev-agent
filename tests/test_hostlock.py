"""Host lock — one run at a time inside a section that inspects and replaces shared Docker state."""

import json
import threading
import time

import pytest

from devagent import hostlock


def test_a_lock_is_exclusive_and_releases(tmp_path):
    lock = tmp_path / hostlock.LOCK_DIR / "egress.lock"
    with hostlock.exclusive(tmp_path, "egress"):
        assert lock.exists()
        with pytest.raises(TimeoutError) as e:
            with hostlock.exclusive(tmp_path, "egress", timeout_s=0.2, poll_s=0.05):
                pass
        assert "held by another run" in str(e.value)
    assert not lock.exists()


def test_a_second_holder_waits_until_the_first_releases(tmp_path):
    order, waited = [], []
    entered = threading.Event()

    def first():
        with hostlock.exclusive(tmp_path, "egress"):
            entered.set()
            order.append("first in")
            time.sleep(0.4)
            order.append("first out")

    t = threading.Thread(target=first)
    t.start()
    entered.wait(2)
    with hostlock.exclusive(tmp_path, "egress", timeout_s=5, poll_s=0.05, on_wait=waited.append):
        order.append("second in")
    t.join()
    assert order == ["first in", "first out", "second in"]
    assert waited == ["egress"], "waiting is announced once"


def test_a_crashed_holder_does_not_wedge_the_host(tmp_path):
    d = tmp_path / hostlock.LOCK_DIR
    d.mkdir(parents=True)
    (d / "egress.lock").write_text(json.dumps({"pid": 999_999_999, "at": time.time()}))
    with hostlock.exclusive(tmp_path, "egress", timeout_s=0.2):
        pass        # reclaimed by pid; no TimeoutError


def test_an_unparseable_lock_is_treated_as_stale(tmp_path):
    d = tmp_path / hostlock.LOCK_DIR
    d.mkdir(parents=True)
    (d / "egress.lock").write_text("{ not json")
    with hostlock.exclusive(tmp_path, "egress", timeout_s=0.2):
        pass


def test_an_uncontended_acquire_reads_no_clock(tmp_path, monkeypatch):
    reads = []
    real = time.monotonic
    monkeypatch.setattr(hostlock.time, "monotonic", lambda: (reads.append(1), real())[1])
    with hostlock.exclusive(tmp_path, "quiet"):
        pass
    assert reads == []
