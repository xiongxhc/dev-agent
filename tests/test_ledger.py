import pytest

from devagent.ledger import Ledger, LedgerCorruption


def test_append_and_read_in_order(tmp_path):
    led = Ledger(tmp_path / "run1")
    led.append({"event": "a", "i": 1})
    led.append({"event": "b", "i": 2})
    events = led.events()
    assert [e["event"] for e in events] == ["a", "b"]
    assert [e["i"] for e in events] == [1, 2]


def test_one_object_per_line(tmp_path):
    led = Ledger(tmp_path / "run1")
    led.append({"event": "a"})
    led.append({"event": "b"})
    lines = (led.path).read_text().strip().splitlines()
    assert len(lines) == 2


def test_tolerates_half_written_trailing_line(tmp_path):
    led = Ledger(tmp_path / "run1")
    led.append({"event": "a"})
    led.append({"event": "b"})
    # simulate a crash mid-append: a truncated, unparseable trailing line
    with led.path.open("a", encoding="utf-8") as f:
        f.write('{"event": "c", "i":')  # no newline, invalid JSON
    events = led.events()
    assert [e["event"] for e in events] == ["a", "b"]


def test_missing_ledger_reads_empty(tmp_path):
    led = Ledger(tmp_path / "fresh")
    assert led.events() == []


def test_corrupt_interior_line_raises_not_silently_dropped(tmp_path):
    # A malformed line that is NOT the last one is real corruption — must be loud, since
    # a resume would otherwise proceed on a ledger silently missing an event.
    led = Ledger(tmp_path / "run1")
    led.append({"event": "a"})
    with led.path.open("a", encoding="utf-8") as f:
        f.write("THIS-IS-NOT-JSON\n")  # corrupt MIDDLE line
    led.append({"event": "c"})
    with pytest.raises(LedgerCorruption):
        led.events()
