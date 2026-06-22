"""Append-only JSONL run state — the audit trail and (later) resume source.
One object per line. A half-written *trailing* line (crash mid-append) is tolerated on
read; a malformed *interior* line is real corruption and is raised loudly rather than
silently dropping an event a resume would depend on."""

import json
from pathlib import Path


class LedgerCorruption(Exception):
    pass


class Ledger:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "ledger.jsonl"

    def append(self, event: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
            f.flush()

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out = []
        for i, line in enumerate(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    break  # benign: a half-written trailing line from a crash mid-append
                raise LedgerCorruption(f"corrupt ledger line {i + 1}: {line!r}")
        return out
