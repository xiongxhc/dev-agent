# devagent/eval/corpus.py
"""The corpus manifest: which fixture PRDs to run, which arms, and N. A small JSON file so a
corpus is data, not code (same spirit as recipe manifests). Fixture paths resolve relative to
the manifest's own directory, so a corpus is portable."""

import json
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_ARMS = ("sdk", "managed", "deepseek")
DEFAULT_ARMS = ("sdk", "managed")   # the third arm is opt-in — a manifest must name it


@dataclass(frozen=True)
class Fixture:
    name: str          # short label, e.g. "hello"
    prd_path: Path     # the PRD file the brain scopes


@dataclass
class Corpus:
    fixtures: list[Fixture]
    arms: list[str] = field(default_factory=lambda: list(DEFAULT_ARMS))
    n: int = 2         # build runs per arm per fixture


def load_corpus(path) -> Corpus:
    """Parse a corpus manifest. Shape:
        {"arms": ["sdk","managed"], "n": 2,
         "fixtures": ["examples/hello.md", {"name": "auth", "prd": "examples/auth.md"}]}
    A fixture is a bare path (name = file stem) or an object with `name`+`prd`. Raises ValueError
    (naming the manifest) on a malformed entry so a typo fails loudly, never silently skips."""
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"corpus manifest {p}: invalid JSON: {e}") from e
    base = p.parent

    arms = data.get("arms", list(DEFAULT_ARMS))
    bad = [a for a in arms if a not in KNOWN_ARMS]
    if bad:
        raise ValueError(f"corpus manifest {p}: unknown arm(s) {bad} (known: {list(KNOWN_ARMS)})")
    n = int(data.get("n", 2))
    if n < 1:
        raise ValueError(f"corpus manifest {p}: n must be >= 1")

    fixtures: list[Fixture] = []
    for item in data.get("fixtures", []):
        if isinstance(item, str):
            prd, name = item, Path(item).stem
        elif isinstance(item, dict) and item.get("prd"):
            prd, name = item["prd"], item.get("name") or Path(item["prd"]).stem
        else:
            raise ValueError(f"corpus manifest {p}: bad fixture entry {item!r}")
        fixtures.append(Fixture(name=name, prd_path=(base / prd).resolve()))
    if not fixtures:
        raise ValueError(f"corpus manifest {p}: no fixtures")
    return Corpus(fixtures=fixtures, arms=list(arms), n=n)
