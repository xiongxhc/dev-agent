"""M5 eval — corpus manifest loading."""

import json

import pytest

from devagent.eval.corpus import load_corpus


def _write(tmp_path, obj):
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(obj))
    return p


def test_load_corpus_bare_paths_and_defaults(tmp_path):
    (tmp_path / "hello.md").write_text("x")
    c = load_corpus(_write(tmp_path, {"fixtures": ["hello.md"]}))
    assert c.n == 2 and c.arms == ["sdk", "managed"]         # defaults
    assert c.fixtures[0].name == "hello"                     # name from stem
    assert c.fixtures[0].prd_path == (tmp_path / "hello.md").resolve()  # resolved vs manifest dir


def test_load_corpus_object_entries_and_overrides(tmp_path):
    c = load_corpus(_write(tmp_path, {
        "arms": ["sdk"], "n": 3,
        "fixtures": [{"name": "auth", "prd": "sub/auth.md"}],
    }))
    assert c.arms == ["sdk"] and c.n == 3
    assert c.fixtures[0].name == "auth"
    assert c.fixtures[0].prd_path == (tmp_path / "sub" / "auth.md").resolve()


def test_unknown_arm_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown arm"):
        load_corpus(_write(tmp_path, {"arms": ["sdk", "gpt"], "fixtures": ["a.md"]}))


def test_bad_fixture_entry_fails_loudly_with_manifest_name(tmp_path):
    with pytest.raises(ValueError, match="corpus.json"):
        load_corpus(_write(tmp_path, {"fixtures": [123]}))


def test_empty_corpus_rejected(tmp_path):
    with pytest.raises(ValueError, match="no fixtures"):
        load_corpus(_write(tmp_path, {"fixtures": []}))


def test_n_below_one_rejected(tmp_path):
    with pytest.raises(ValueError, match="n must be"):
        load_corpus(_write(tmp_path, {"n": 0, "fixtures": ["a.md"]}))
