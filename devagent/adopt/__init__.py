"""Adopt — point dev-agent at a repo it did not build.

Four stages, each a gate in the orchestrator idiom: survey (deterministic) -> map (agentic,
read-only) -> assemble (brain + two new gates) -> prove (deterministic rebuild). The output is a
synthetic run dir shaped exactly like a build-system run, so `update-system` consumes it with no
new plumbing.

The source repo is never written to. Editing adopted code is a separate step
(`run --build --repo`), which consumes what this produces.
"""

from .assemble import Fragment, GateFailure, compose, contract_id, needed_recipe
from .survey import ServiceRoot, Survey, survey_repo, survey_repos

__all__ = ["Fragment", "GateFailure", "compose", "contract_id", "needed_recipe",
           "ServiceRoot", "Survey", "survey_repo", "survey_repos"]
