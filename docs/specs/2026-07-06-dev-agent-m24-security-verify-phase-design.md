# Dev-Agent M24: Security Verify Phase (Red-Team) — Design

**Date:** 2026-07-06
**Status:** Draft — design approved in session
**Depends on:** M23 system-level repair loop
([`2026-07-06-dev-agent-system-repair-loop-design.md`](./2026-07-06-dev-agent-system-repair-loop-design.md))
— this phase's findings feed that loop.
**Scope:** a new dev-agent verify phase. Not a new agent, and not team-lead's job (team-lead
watches runs from the outside — rerun/kill/digest — and never inspects an app's internals or
gates a build). Probing a built app for security defects is build verification, which is
dev-agent's.

## Problem

Acceptance and integration checks verify the contract was **met** — they assert intended
behavior. A security defect is *unintended* behavior the contract never forbade, so no derived
check can catch it. Live evidence (2026-07-03): a dev-agent-built app's `POST /auth/register`
accepted a `role` field and returned `201` with an admin JWT — privilege escalation at
registration (mass assignment). Every functional check passed. The class this misses:
mass-assignment, missing authorization on protected routes, cross-user IDOR, default/weak
credentials — all invisible to "did the route return the contracted shape?".

## Decision summary

| Question | Decision |
|---|---|
| What it is | A verify step that runs against the **brought-up preview** after functional verification passes, reusing the existing `base_urls` and the synthesized `AuthFlow`. Invoked directly by the orchestrator at its call site (see Placement) — **not** appended to `build_pipeline_phases`, whose per-service sub-runs execute with `deploy=False` *before* any bring-up, so `base_urls` never exist there and the phase would silently no-op on every service |
| Placement | Two explicit call sites, both where a live `base_urls` exists: in `build_system`, after integration passes and before `succeeded` is emitted (feeding the M23 loop); and in a single-service `run`, after `DeployPhase`. No new bring-up for the probe pass — it probes the system that's already up. **Auto-repair of a gating finding exists only in the system-build flow** (where the M23 loop lives); on a single-service run a gating finding fails the run with the evidence — reported, not auto-repaired, since the single-run path has no post-deploy repair loop |
| Probe library (deterministic) | Keyed off the frozen contract + recipe so probes are app-specific: **mass-assignment** (inject `role`/`is_admin`/`isVerified` into POST/PUT bodies, check reflection in response or token) · **missing-authz** (declared-protected route with no token → expect 401/403) · **IDOR** (authed as user A, read/mutate a resource owned by user B → expect 403/404, not 200) · **weak-registration/default-creds** · **verb/method tampering** |
| LLM's job | Expand app-specific probes from the contract and **classify** responses (is this 200 a vuln or expected?); draft the finding text. **Same fail-safe posture as the rest of the fleet:** the LLM triages and expands; a curated **deterministic ruleset** decides which findings gate. The LLM cannot invent a gating finding, cannot block a run alone, and if the API is down the deterministic probes still run and still gate |
| Gating + repair | **High-confidence deterministic findings** (privilege escalation, missing authz on a declared-protected route, cross-user IDOR) **FAIL verification** and feed M23's repair loop — so dev-agent repairs the `role=admin` bug, not just reports it. Mechanically: each gating finding is rendered as one failing step `{service, route, detail: evidence+remediation}` — the exact shape M23's `implicated_nodes` consumes to attribute the node. The `repair_context` handed to that node's executor is the **whole report** M23 renders (`render(report, node)` — all failing steps, its own marked), NOT the lone finding: M23's mis-attribution mitigation depends on the executor seeing the cross-service picture. M23's combined re-verify re-runs the integration suite **and** this security phase (M23 "Re-verification"), so the repaired node must re-pass functional **and** security verification. Lower-confidence / LLM-only findings are **advisory** (in the report, don't gate) |
| Auth | Reuses `auth_flow_from_contract` (already synthesized for auth-aware checks). IDOR/authz probes need two principals: the phase registers a **second user** via the same flow when registration is open; when it is closed/invite-only, it obtains principal B via the contract's declared provisioning path (seed / admin-create) if one exists. If no second principal can be obtained, the IDOR/authz probe class is reported **not-run** (an explicit advisory line in the report), never silently skipped — the operator must see the coverage gap rather than read a clean report as "no IDOR" |
| Intentional-open escape hatch | Scoped to a **(route, probe-class)** pair, never a whole endpoint. A design that *wants* open registration declares *that property* — "unauthenticated signup is allowed on `/auth/register`" — which suppresses only the missing-authz / weak-registration gate on that route. It does **not** suppress the mass-assignment probe there: "anyone may sign up" and "a signup may set its own `role`" are different properties, and a public `/auth/register` that accepts `role=admin` is still the exact privilege escalation this phase exists to catch. An endpoint-wide "open" flag would re-open the July-3 bug through the escape hatch |
| Out of scope | Network/infra/host scanning (that's security-agent's posture-audit turf — different target, different agent) · fuzzing · any run against a non-preview target. Active probes mutate state (they register a second principal and inject `role=admin` / cross-user writes), so they are safe **only** against a disposable target. On a run whose preview is **kept** (a clean / advisory-only success), the probed stack is therefore torn down and re-brought-up once more before hand-off, so the delivered preview is pristine — the probes never leave their second user and tampered data in a target the user keeps. That one extra bring-up on the success path is the price of active probing; "no new bring-up" holds only for the probe pass itself |

## Flow

```
system-build:  architect → builds → bring_up → integration (ok)
                                                     │
                                                     ▼   [NEW]
                                          security verify phase
                                          ── deterministic probes (contract+recipe keyed)
                                          ── second principal via AuthFlow (IDOR/authz)
                                          ── LLM triage/expand + classify
                                                     │
                          ┌──────────────────────────┴───────────────────────┐
              high-confidence deterministic finding            no gating finding
                          │                                                   │
              FAIL verify → M23 repair loop           advisory / clean → report
              (repair_context = whole report)         → teardown + clean re-bring-up
                          │                            → run succeeds (pristine preview)
              teardown → rebuild node → combined re-verify (functional + security)
```

## Components

1. `security/probes.py` — the deterministic probe library. Each probe: `(base_urls, contract,
   auth) -> list[Finding]`. Pure enough to unit-test against a fixture app.
2. `SecurityVerifyPhase` — invoked by the orchestrator at a live-`base_urls` call site
   (`build_system` after integration; single-run after `DeployPhase`), **not** appended to
   `build_pipeline_phases`: that per-service list runs `deploy=False` before bring-up, so it
   never holds `base_urls` and the phase would no-op there. Runs only when `base_urls` and at
   least one probeable contract exist.
3. `security/ruleset.py` — the deterministic gating policy: maps finding kind → gate/advisory.
   The only thing that can fail a run. Conservative by construction.
4. `security/triage.py` — LLM seam (`llm.py` structured output): expands app-specific probes,
   classifies ambiguous responses, drafts finding prose. Fail-safe: skipped ⇒ deterministic
   probes and gating still run.
5. `Finding` schema — `{kind, service, route, method, severity, confidence, evidence,
   remediation}`. `service` is known at probe time (each probe targets one `base_urls` entry)
   and is what M23's attribution maps to a node. Gating findings render as failing steps
   (`{service, route, detail: evidence+remediation}`) for M23's `implicated_nodes`; the same
   detail becomes the repair `repair_context`.

## Known edge: false positive gating a legitimate design

The bootstrap-admin case. Mitigations: (a) the gating ruleset covers only unambiguous classes;
(b) the contract escape hatch above lets a design declare an intentionally-open endpoint;
(c) a mis-gate costs one repair pass, then the finding recurs and the run ends
`integration_failed` — M23's four-value status vocabulary is unchanged; the gating security
findings are recorded on `SystemReport` so the operator sees the cause was security — a
bounded, diagnosable outcome, not silent breakage and not a new status consumers must learn.

## Testing

- `probes.py` against two fixture apps — a vulnerable one (the July 3 `role=admin` app as a
  regression fixture) and a safe one — asserting the vuln app trips exactly the expected
  findings and the safe app trips none.
- `ruleset.py`: gating vs advisory classification per finding kind; the contract escape hatch
  suppresses gating on a declared-open endpoint.
- Triage returns only allowlisted finding kinds (schema-enforced); API-down ⇒ deterministic
  probes still produce gating findings.
- Integration with M23: a gating finding renders as a failing step that M23's attribution maps
  to the vulnerable node; the node's `repair_context` is M23's whole-report render (not the lone
  finding); a repaired app re-passes both functional and security verification via M23's combined
  `reverify(design, base_urls)` (integration suite + this phase merged); an exhausted security
  repair terminates `integration_failed` with the findings recorded.
- Closed-registration app: no second principal obtainable → IDOR/authz probes report **not-run**
  (explicit advisory), not a silent pass. Open `/auth/register` declared intentionally-open →
  the missing-authz gate is suppressed but the mass-assignment `role=admin` probe still gates.
- Live: run the system-polls example with an injected mass-assignment bug; confirm the phase
  gates, M23 repairs it, and the preview comes up clean.
