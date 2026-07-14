"""`devagent run <prd>` — the brain pipeline: scope -> plan [-> build -> deploy].

Each phase is a bounded LLM call gated deterministically. Brain phases run on the host
(NullSandbox); the build phase uses a real Docker sandbox (SdkExecutor or ManagedExecutor).
Produced artifacts (scope.json / plan.json) are written to runs/<id>/ for inspection."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import egress
from .budget import Budget
from .config import Config
from .deploy import DeployGate
from .executor_sdk import SdkExecutor
from .ledger import Ledger
from .managed_executor import ManagedExecutor
from .orchestrator import SUCCEEDED, Orchestrator
from .phase_gates import PlanGate, ScopeGate, VerifyGate
from .phases.build import BuildPhase
from .phases.deploy import DeployPhase
from .phases.plan import FrozenPlanPhase, PlanPhase
from .phases.scope import FrozenScopePhase, ScopePhase
from .report import write_report
from .sandbox import NullSandbox
from .system_build import (build_system, make_bring_up, make_run_node,
                           make_update_build_service, update_system)
from .verifier import BuildVerifier


def _new_run_id() -> str:
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def build_pipeline_phases(input_path, *, build=False, deploy=True, out_dir=None, run_id=None,
                          executor=None, verifier=None, answers_path=None, consumed_contracts=(),
                          provided_contracts=(), scope=None, plan=None, repair_context=None,
                          context_kind="repair"):
    """The scope->plan[->build[->deploy]] phase list + gate map. Shared by `run` and the M20
    `build-system` per-service sub-runs so both assemble the pipeline identically. Sub-runs
    pass deploy=False: previews are pointless there, and their fixed devagent-preview-<name>
    container names collide across concurrent sibling builds — system bring-up starts the
    real containers instead (M21). `consumed_contracts` (M21) are the node's consumed
    contract-spec dicts, `provided_contracts` the ones it must implement — both forwarded
    into BuildPhase -> BuildRequest -> enrich_scope. `scope` (one-flow, 2026-07-03): a
    precomputed ProjectScope — the phase list starts with FrozenScopePhase instead of an LLM
    ScopePhase; system sub-builds pass the scope derived from the architect's design so the
    design is decided exactly once. `plan` (M23): likewise freezes the plan phase via
    FrozenPlanPhase for a repair sub-run reloading the persisted plan — re-planning could
    restructure what integration already half-validated. `repair_context` (M23) forwards into
    BuildPhase to seed the first BuildRequest with prior verify/integration diagnostics.
    `context_kind` (M25) says what `repair_context` IS — "repair" (verify diagnostics) or
    "update" (a user change request) — forwarded into BuildPhase to pick the prompt framing."""
    first = FrozenScopePhase(scope) if scope is not None \
        else ScopePhase(input_path, answers_path=answers_path)
    plan_phase = FrozenPlanPhase(plan) if plan is not None else PlanPhase()
    phases = [first, plan_phase]
    gates = {"scope": ScopeGate(), "plan": PlanGate()}
    if build:
        # BuildPhase owns the repair loop: build -> rebuild-from-source verify -> repair
        # (cap 2), then emits the VerifyReport. VerifyGate is the trusted final check.
        phases.append(BuildPhase(executor=executor, workdir=str(out_dir), run_id=run_id,
                                 verifier=verifier, max_repairs=2,
                                 consumed_contracts=consumed_contracts,
                                 provided_contracts=provided_contracts,
                                 repair_context=repair_context,
                                 context_kind=context_kind))
        gates["build"] = VerifyGate()
        if deploy:
            # Deploy the built app to a local preview server -> URL (gated on it actually answering).
            phases.append(DeployPhase(workdir=str(out_dir)))
            gates["deploy"] = DeployGate()
    return phases, gates


def _eval(args) -> int:
    """M5 A/B eval: freeze scope+plan per fixture, build each arm N times, score, tabulate.
    Resumable under runs/eval/<id>/. Real builds — needs Docker + tokens."""
    from .eval.corpus import load_corpus
    from .eval.report import write_report as write_eval_report
    from .eval.runner import (EvalRunner, build_default_brain_fn, build_default_build_fn,
                              build_default_judge_fn, default_arm_available)

    if not Path(args.corpus).is_file():
        print(f"error: corpus manifest not found: {args.corpus}", file=sys.stderr)
        return 2
    cfg = Config.load()
    corpus = load_corpus(args.corpus)
    eval_id = args.id or f"eval-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    eval_dir = cfg.runs_dir / "eval" / eval_id
    session_hr = float(os.environ.get("DEVAGENT_SESSION_HR_USD", 2.0))

    n_builds = len(corpus.fixtures) * len([a for a in corpus.arms if default_arm_available(a)]) * corpus.n
    print(f"eval {eval_id}: {len(corpus.fixtures)} fixtures × {corpus.arms} × N={corpus.n} "
          f"= up to {n_builds} real contained builds (resumable under {eval_dir})")

    runner = EvalRunner(
        eval_dir, corpus,
        brain_fn=build_default_brain_fn(cfg), build_fn=build_default_build_fn(cfg),
        judge_fn=build_default_judge_fn(), session_hr_usd=session_hr,
        arm_available=default_arm_available)
    result = runner.run()
    jp, hp = write_eval_report(eval_dir, result)
    print(f"  -> eval report: {hp}\n  -> eval json:   {jp}")
    return 0


def _system_security(ledger):
    """The security-verify seam shared by build-system and update-system (M24 wiring)."""
    from .security.phase import SecurityVerifyPhase

    findings: list = []

    def verify(design, base_urls):
        result = SecurityVerifyPhase(design).verify(base_urls)
        findings[:] = [f.model_dump() for f in result.findings]
        if ledger is not None and result.not_run:
            ledger.append({"event": "security_not_run", "classes": sorted(set(result.not_run))})
        return result.gating_steps

    return verify, findings


def _write_system_report(run_dir, report) -> None:
    (run_dir / "system-report.json").write_text(json.dumps({
        "title": report.title, "status": report.status, "build_ok": report.build_ok,
        "services": {k: {"status": v.status, "detail": v.detail}
                     for k, v in report.node_results.items()},
        "integration": [dict(s) for s in report.integration.steps] if report.integration else None,
        "urls": report.urls,
        "repairs": report.repairs,
        "findings": report.findings,
    }, indent=2))


def _build_system(args) -> int:
    if not Path(args.input).is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2
    cfg = Config.load()
    run_id = _new_run_id()
    run_dir = cfg.runs_dir / run_id
    ledger = Ledger(run_dir)
    ledger.append({"event": "input", "path": args.input})
    budget = Budget(cfg.max_tokens, cfg.max_seconds, cfg.max_retries, max_cost_usd=cfg.max_cost_usd)

    security_verify, security_findings = _system_security(ledger)

    report = build_system(
        args.input, budget=budget, ledger=ledger,
        run_node=make_run_node(run_dir, budget, ledger),
        bring_up=make_bring_up(run_dir), run_dir=run_dir,
        max_system_repairs=cfg.max_system_repairs,
        security_verify=security_verify, security_findings=security_findings)
    _write_system_report(run_dir, report)
    print(f"{run_id} {report.status}")
    print(f"  -> services: " + ", ".join(
        f"{k}={v.status}" if v.status == "succeeded" else f"{k}={v.status} ({v.detail})"
        for k, v in report.node_results.items()))
    for svc, url in report.urls.items():
        print(f"  -> preview {svc}: {url}")
    print(f"  -> report: {run_dir / 'system-report.json'}")
    return 0 if report.status == "succeeded" else 1


def _update_system(args) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / "design.json").is_file():
        print(f"error: {run_dir} has no design.json — not a prior system run", file=sys.stderr)
        return 2
    if not Path(args.change).is_file():
        print(f"error: change file not found: {args.change}", file=sys.stderr)
        return 2
    cfg = Config.load()
    ledger = Ledger(run_dir)      # append-only: the update continues the run's event trail
    ledger.append({"event": "update_input", "path": args.change})
    budget = Budget(cfg.max_tokens, cfg.max_seconds, cfg.max_retries,
                    max_cost_usd=cfg.max_cost_usd)
    security_verify, security_findings = _system_security(ledger)
    change_request = Path(args.change).read_text()

    report = update_system(
        run_dir, args.change, budget=budget, ledger=ledger,
        run_node=make_run_node(run_dir, budget, ledger,
                               build_service=make_update_build_service(change_request)),
        bring_up_factory=lambda preserve: make_bring_up(run_dir, preserve_data=preserve),
        max_system_repairs=cfg.max_system_repairs,
        security_verify=security_verify, security_findings=security_findings)
    _write_system_report(run_dir, report)
    print(f"{run_dir.name} {report.status}")
    for svc, url in report.urls.items():
        print(f"  -> preview {svc}: {url}")
    print(f"  -> report: {run_dir / 'system-report.json'}")
    return 0 if report.status == "succeeded" else 1


def _single_run_design_shim(artifacts):
    """A SecurityVerifyPhase-shaped design for the single-run preview. Single runs hold no
    probeable openapi contract today (build_pipeline_phases passes no provided_contracts), so
    the shim carries empty contracts and the phase no-ops. The single-run security pass
    activates only if/when a single run gains an openapi contract; today it is a no-op guard so
    the call site exists at the correct spot (after DeployPhase)."""
    from types import SimpleNamespace
    return SimpleNamespace(contracts=[], services=[])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devagent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run the pipeline on a PRD file")
    run_p.add_argument("input", help="path to a PRD/requirement file")
    run_p.add_argument("--build", action="store_true",
                       help="run the contained build phase (SdkExecutor; needs Docker + spends tokens)")
    run_p.add_argument("--answers", help="answers file for a prior clarification round")

    eval_p = sub.add_parser("eval", help="A/B eval: run a fixture corpus across build arms (M5)")
    eval_p.add_argument("corpus", help="path to a corpus manifest (JSON)")
    eval_p.add_argument("--id", default=None, help="eval id / resume dir name (default: timestamped)")

    bs_p = sub.add_parser("build-system", help="M20: design + build a multi-service system from a PRD")
    bs_p.add_argument("input", help="path to a PRD/requirement file")

    us_p = sub.add_parser("update-system",
                          help="M25: apply a change request to a previously built system, in place")
    us_p.add_argument("run_dir", help="prior run dir (holds design.json + services/)")
    us_p.add_argument("change", help="path to the change-request text file")

    args = parser.parse_args(argv)

    if args.cmd == "eval":
        return _eval(args)

    if args.cmd == "build-system":
        return _build_system(args)

    if args.cmd == "update-system":
        return _update_system(args)

    if not Path(args.input).is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    cfg = Config.load()
    run_id = _new_run_id()
    run_dir = cfg.runs_dir / run_id
    ledger = Ledger(run_dir)
    ledger.append({"event": "input", "path": args.input})

    # SdkExecutor self-contains its own disposable Docker container, so the
    # orchestrator's NullSandbox still suffices for the host-side brain phases.
    out_dir = run_dir / "out"
    # Egress allowlist for OUR containers (verify always; the sdk build too). The managed
    # arm builds on Anthropic's cloud sandbox, so it doesn't use our network — but verify
    # still re-checks the pulled output in our egress-contained container.
    network = proxy = None
    if args.build and cfg.egress:
        network, proxy = egress.ensure()
        ledger.append({"event": "egress", "network": network, "proxy": proxy})
    # The A/B seam: pick the build arm. Everything downstream (verify/acceptance/repair) is shared.
    executor = None
    if args.build:
        ledger.append({"event": "executor", "kind": cfg.executor})
        if cfg.executor == "managed":
            executor = ManagedExecutor()
        else:
            executor = SdkExecutor(network=network, proxy_url=proxy, model=cfg.build_model)
    verifier = BuildVerifier(network=network, proxy_url=proxy) if args.build else None
    phases, gates = build_pipeline_phases(
        args.input, build=args.build, out_dir=out_dir, run_id=run_id,
        executor=executor, verifier=verifier, answers_path=args.answers)

    orch = Orchestrator(
        phases=phases,
        gates=gates,
        budget=Budget(cfg.max_tokens, cfg.max_seconds, cfg.max_retries,
                      max_cost_usd=cfg.max_cost_usd),
        ledger=ledger,
        sandbox=NullSandbox(),  # brain phases run on host; SdkExecutor contains its own
    )
    status = orch.run()

    # Persist the validated artifacts produced so far for inspection.
    for name, artifact in orch.artifacts.items():
        if hasattr(artifact, "model_dump_json"):
            (run_dir / f"{name}.json").write_text(artifact.model_dump_json(indent=2))

    # Always write a run report (even on failure — it reads the full ledger trail).
    deploy_art = orch.artifacts.get("deploy")
    preview_url = deploy_art.url if deploy_art is not None else None
    acc_path = run_dir / "out" / ".devagent" / "acceptance.json"
    acceptance = json.loads(acc_path.read_text()).get("checks") if acc_path.is_file() else None
    report_path = write_report(run_dir, ledger.events(), run_id,
                               preview_url=preview_url, acceptance=acceptance)

    # M24: red-team the single-run preview after DeployPhase. A gating finding fails the run
    # with the evidence (the single-run path has no post-deploy repair loop — reported, not
    # auto-repaired). deploy_art.urls maps target name -> url; the phase reuses it as base_urls.
    # With the current empty shim (single runs hold no openapi contract) this never fires, but
    # the wiring sits at the correct spot for when a single run gains a probeable contract.
    if args.build and status == SUCCEEDED and deploy_art is not None and deploy_art.urls:
        from .security.phase import SecurityVerifyPhase
        sec = SecurityVerifyPhase(_single_run_design_shim(orch.artifacts)).verify(deploy_art.urls)
        if sec.gating_steps:
            for s in sec.gating_steps:
                print(f"  -> SECURITY GATE: {s['route']}: {s['detail']}", file=sys.stderr)
            (run_dir / "security-findings.json").write_text(
                json.dumps([f.model_dump() for f in sec.findings], indent=2))
            return 1

    if status != SUCCEEDED:
        scope = orch.artifacts.get("scope")
        clar = getattr(scope, "clarifications", None) if scope is not None else None
        if clar:
            print("  -> needs clarification before building:", file=sys.stderr)
            for q in clar:
                print(f"     - {q}", file=sys.stderr)
            print("  -> answer these in a file and re-run with --answers <file>", file=sys.stderr)

    print(f"{run_id} {status}")
    plan = orch.artifacts.get("plan")
    if status == SUCCEEDED and plan is not None:
        print(f"  -> {len(plan.tasks)} tasks; artifacts in {run_dir}")
    if status == SUCCEEDED and args.build:
        print(f"  -> built + verified app in {run_dir / 'out'}")
    if preview_url:
        print(f"  -> preview: {preview_url}")
    print(f"  -> report: {report_path}")
    return 0 if status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
