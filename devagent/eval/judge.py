# devagent/eval/judge.py
"""The blinded per-criterion judge. Reads a digest of the BUILT repo (file tree + key source,
truncated) and scores it 1–5 per rubric criterion against the spec — with NO signal about which
arm produced it (the caller strips the arm; nothing here mentions sdk/managed). Deterministic
harness, model-in-the-loop only for the qualitative score; acceptance stays the authoritative one."""

import json
from pathlib import Path

from ..llm import generate_structured
from .schema import RUBRIC, JudgeVerdict

_SKIP_DIRS = {"node_modules", ".git", "dist", ".devagent", "__pycache__", ".venv"}
_MAX_FILES = 40
_MAX_BYTES_PER_FILE = 2000


def digest_repo(repo_path, max_files: int = _MAX_FILES) -> str:
    """A compact, arm-agnostic snapshot of the built source: the file tree plus a truncated head
    of each source file (build outputs and vendored deps skipped). Bounded so the judge prompt
    stays small and comparable across builds."""
    root = Path(repo_path)
    if not root.is_dir():
        return "(no repo produced)"
    files = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _SKIP_DIRS for part in f.relative_to(root).parts):
            continue
        files.append(f)
    files = files[:max_files]
    parts = ["FILE TREE:"]
    parts += [f"  {f.relative_to(root)}" for f in files]
    parts.append("\nKEY FILES (truncated):")
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:_MAX_BYTES_PER_FILE]
        except OSError:
            continue
        parts.append(f"\n--- {f.relative_to(root)} ---\n{text}")
    return "\n".join(parts)


_PROMPT = """\
You are grading a generated application against its specification. You do NOT know how it was
built — grade only what is in front of you, on the merits.

SPECIFICATION (what was requested):
{spec}

BUILT APPLICATION (file tree + truncated source):
{digest}

Score each criterion from 1 (poor) to 5 (excellent), with a one-line reason:
{rubric}

Then give an `overall` score (1–5). Be calibrated: 3 is a competent, complete build; reserve 5
for genuinely excellent work and 1–2 for missing or broken deliverables.
"""


def judge_build(spec_summary: str, repo_path, client=None) -> JudgeVerdict:
    """Score one built repo, blinded. `client` is injectable for tests (no live model call)."""
    prompt = _PROMPT.format(
        spec=spec_summary,
        digest=digest_repo(repo_path),
        rubric="\n".join(f"  - {c}" for c in RUBRIC),
    )
    verdict, _usage = generate_structured(prompt, JudgeVerdict, client=client)
    return verdict


def spec_summary(scope) -> str:
    """A stable, arm-agnostic spec digest for the judge prompt, from the frozen ProjectScope."""
    if hasattr(scope, "model_dump"):
        scope = scope.model_dump()
    return json.dumps(scope, indent=2, default=str)[:4000]
