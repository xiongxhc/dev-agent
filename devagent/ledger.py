"""Append-only JSONL run state — the audit trail and (later) resume source.
One object per line; a half-written trailing line is tolerated on read so a crash
mid-append cannot corrupt the whole ledger."""

import json
from pathlib import Path


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
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # tolerate a malformed (e.g. half-written) line; skip it
                continue
        return out
