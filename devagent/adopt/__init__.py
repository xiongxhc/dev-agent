"""Adopt — point dev-agent at a repo it did not build.

Four stages: survey (deterministic) -> map (agentic, read-only) -> assemble (composition plus
two gates) -> prove (deterministic rebuild). The output is a run dir shaped like a build-system
run, so `update-system` can load it.

The source repo is never written to.
"""

from .assemble import Fragment, GateFailure, compose, contract_id, needed_recipe
from .lane import AdoptResult, adopt
from .mapper import make_mapper, map_service
from .prove import PROVEN, UNMAPPABLE, UNPROVEN, Verdict, prove, prove_service
from .report import write_report
from .survey import ServiceRoot, Survey, survey_repo, survey_repos

__all__ = ["Fragment", "GateFailure", "compose", "contract_id", "needed_recipe",
           "AdoptResult", "adopt",
           "make_mapper", "map_service",
           "PROVEN", "UNMAPPABLE", "UNPROVEN", "Verdict", "prove", "prove_service",
           "write_report",
           "ServiceRoot", "Survey", "survey_repo", "survey_repos"]
