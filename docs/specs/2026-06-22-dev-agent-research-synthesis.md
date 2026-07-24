# DevAgent — Research Synthesis & Revised Architecture (Build-Both + A/B)

**Date:** 2026-06-22
**Inputs:** 4 parallel research teammates — `managed-path`, `selfbuilt-path`, `prior-art`, `abtest-eval`
**Supersedes the single-backend assumption in:** [`2026-06-22-dev-agent-design.md`](./2026-06-22-dev-agent-design.md)
**Decision:** build **both** execution backends behind one shared harness; resolve the two genuine unknowns (output quality, cost) by A/B test.

---

## 0. The one-line conclusion

All four lanes converged: the spec's **deterministic harness + bounded Claude phases + gates-as-code + file-ownership fan-out + honest-failure** is the right architecture — it's what the best systems converged on and what Anthropic's own docs recommend. The new information is that there are **two viable headless backends**, not one, and the right move is to make the **execution engine a swappable seam** and let evidence pick.

## 1. The three "agent" products, finally disambiguated

| Product | Headless? | Role for us |
|---|---|---|
| Claude Code **Agent Teams** | No (interactive terminal only) | Dev-time tool (how we build/review DevAgent) — not a runtime |
| Claude **Agent SDK** | Yes (you own loop+sandbox) | **Self-built backend** (`SdkExecutor`) |
| Claude **Managed Agents** | Yes (hosted REST; cloud *or* self-hosted sandbox) | **Managed backend** (`ManagedExecutor`) |

Spec correction: the original §2 implied the SDK is the only headless path. Managed Agents is equally headless and replaces most of the hand-built plumbing.

## 2. Architecture: shared harness, one swappable seam

Everything is shared **except the build execution engine**. intake → spec → plan are identical bounded Claude calls; verify → deploy → report → gates → ledger → budget → eval are shared scoring. Only **who turns a frozen Spec+Plan into a built repo** differs.

```
PRD/URL ─▶ intake ─▶ spec ─▶ plan ─▶ [ Executor ] ─▶ verify ─▶ deploy ─▶ report ─▶ eval
           └────────── shared, deterministic-gated ──────────┘     └─── shared ───┘
                                          │
                          ┌───────────────┴───────────────┐
                    SdkExecutor                      ManagedExecutor
              (Agent SDK + subagents,           (Managed Agents multiagent
               your Docker --rm sandbox)         coordinator, hosted/self-hosted sandbox)
```

```python
class Executor(Protocol):
    def build(self, req: BuildRequest) -> BuildResult: ...
# BuildRequest: frozen spec, frozen plan, workdir, shared Budget, run_id
# BuildResult: repo_path, success(claimed, NOT trusted), tokens_in/out, wall_clock_s,
#              transcript_path, tool_calls, cost_usd, error
```

**Three fairness rules** (from `abtest-eval`) that make the A/B valid:
1. Freeze `Spec`+`Plan`; feed *identical* `BuildRequest` to both arms (removes upstream LLM variance).
2. Gates **never** read the executor's `success` — they re-run deterministically on `repo_path`.
3. Pull source out and run verify/deploy in **one shared sandbox** for both arms (neutralizes toolchain confounds).

## 3. What each backend gives vs costs (build-vs-buy)

Priors already settle most dimensions; only **output quality** and **cost** are empirical TBDs the A/B exists to resolve.

| Dimension | SdkExecutor (build) | ManagedExecutor (buy) |
|---|---|---|
| Time-to-build / ops | — (you build sandbox, loop, ledger, resume) | **wins** (platform provides all of it) |
| Control granularity | **wins** (in-process hooks, own the loop, per-command gating) | between-turn gating only; per-tool not per-command |
| Observability | every tool call in-process | **strong** (hosted Console tracing + external event log) |
| Build fan-out | SDK subagents (hierarchical, report-to-lead) | **stronger** (`multiagent` coordinator, shared FS across context-isolated threads, per-agent models, ≤25 threads) |
| Lock-in | **wins** (portable, your infra) | Anthropic-specific agent/env/session/event model |
| Local-first ethos | **wins** (your box) | cloud-brained; *self-hosted sandbox* keeps execution+egress local but brain stays in Anthropic's control plane |
| ZDR / HIPAA | possible | **not eligible** (stateful by design) |
| Output quality | **TBD — the A/B** | **TBD — the A/B** |
| Cost | **TBD — the A/B** | **TBD — the A/B** |

**Cost facts (verified, `managed-path`):** Opus 4.8 = $5/MTok in, $25/MTok out, cache-read 0.1×; Managed adds **$0.08/session-hour, metered only while `running`** (idle is free). Worked example: 1-hr Opus session, 50k in/15k out ≈ **$0.70**. Web search $10/1k. SdkExecutor cost = raw tokens (same model pricing, automatic prompt caching ~0.1× on cache hits) + your Docker compute. Report **both** model-token cost (apples-to-apples) and all-in cost.

## 4. Corrections & hardening to fold into the spec + M1 plan

**Factual / safety (do before wiring any brain):**
- **C1.** Permission posture: use `permission_mode="dontAsk"` + explicit `allowed_tools` (unlisted → denied), **not** `bypassPermissions`. Reason: subagents inherit `bypassPermissions` and it **can't be overridden per-subagent** → a build subagent would silently get full system access. (`selfbuilt-path`, permissions doc)
- **C2.** Adopt Anthropic's hardened `docker run` flags in M1 T3 now, not M2: `--cap-drop ALL --security-opt no-new-privileges --pids-limit --memory --cpus --user 1000:1000 --network none`, `out/` the only rw mount. (secure-deployment doc)
- **C3.** Start network-**closed** (`--network none`) in M1 (no-op needs none) + add an egress test; M2 *relaxes* to a unix-socket **proxy allowlist outside** the container (cleaner than in-container iptables; solves "installs pull from many hosts"). FS-isolation-without-network-isolation is an explicit escape path.
- **C4.** Credentials via **proxy-injection** — the brain never sees the Vercel/Anthropic token; route sampling via `ANTHROPIC_BASE_URL`, deploy creds injected by an out-of-sandbox proxy. (Both backends already support this; Managed via vaults.)
- **C5.** Build subagents must NOT include `Agent`/`Task` in their tools (prevents the new ≤5-level nested-spawn → uncontrolled recursion); parent `query()` must include `Agent` in `allowed_tools`.

**Quality levers (from `prior-art` + `selfbuilt-path`):**
- **Q1.** Constrain to **ONE opinionated stack + a real scaffold template** (v0's edge: Vite/Next + Tailwind + shadcn). Don't let the model pick the framework — biggest first-pass-quality lever, fewer hallucinated deps, simpler gates.
- **Q2.** **Repair cap = 2–3 attempts, hard.** Feed *targeted* gate diagnostics into repair, never the whole codebase (bolt.new burned 7–20M tokens/bug doing the latter). Exhaustion → clean failure.
- **Q3.** **Pin deps; any import outside the lockfile = build-gate failure** (kills the hallucinated-dependency class).
- **Q4.** **Reason-then-apply split** in build subagents (decide the change, then a narrow step writes it to disk — GPT-Pilot reliability trick).
- **Q5.** Verifier **drives the running app** deterministically (Playwright: boot + route 200 + selector + no console-fatal + screenshot); LLM-judge only for "matches reference intent," off the critical path.
- **Q6.** Model-tier the fan-out via `AgentDefinition.model` (haiku/sonnet for scaffold/lint-fix/tests, opus for spec/plan) — native hybrid-brain, captures most cost savings with zero router code. Keep prompts stable across a run so prompt-cache prefixes hold (~0.1× on hits).

**Deploy gotcha (`selfbuilt-path`):**
- **D1.** Behind a `trycloudflare` quick tunnel, deploy the **production build** (`next build && next start` / static export), **not** the dev server — quick tunnels don't support SSE and cap at 200 concurrent; HMR/SSE will fail the smoke check confusingly.

## 5. Revised milestones (build-both + A/B)

The plan "barely changes — it grows a second Executor and an eval-corpus runner" (`abtest-eval`).

1. **M1 — Skeleton + sandbox (shared).** As planned, hardened per C2/C3 (`--network none` default + egress test, official flags). Proves containment. *No tokens.*
2. **M2 — Shared pipeline + `SdkExecutor` on a PRD fixture.** intake→spec→plan→build(SDK subagents)→verify, stop at green build + acceptance checks locally. Bake in Q1–Q5, C1/C4/C5.
3. **M3 — Deploy + report.** Tunnel adapter (D1), screenshot, run-report → full `PRD → live URL` on `SdkExecutor`.
4. **M4 — `ManagedExecutor`.** Second `Executor` impl on Managed Agents (`multiagent` coordinator, `limited` networking, custom-tool deploy, between-turn gates). Same `BuildRequest`/`BuildResult`.
5. **M5 — Eval corpus + A/B run.** 6–8 fixtures (easy→hard PRD + reference-URL), **N=5/arm**; unbiased pass@k, blinded per-criterion judge + deterministic checks + SSIM for URL clones, dual cost normalization. Score the §3 matrix; the two TBDs resolve.

PRD path is primary; **reference-URL intake is the hard, underexplored path** (no surveyed system does it well) → keep it to the URL fixtures / later, as the spec's build order already does.

## 6. What the A/B decides

If `SdkExecutor` matches `ManagedExecutor` on **quality at comparable-or-lower cost**, the control/lock-in/local-first/ZDR axes make self-built the pick for this repo. If Managed is **materially better on quality**, you weigh that against lock-in/ethos with real numbers — not vibes. Either way the shared harness + the logic that matters (phase prompts, schemas, gates, deploy, report, eval) is **yours and reused across both**.

## 7. Sources (consolidated)
Agent SDK: [overview](https://code.claude.com/docs/en/agent-sdk/overview) · [subagents](https://code.claude.com/docs/en/agent-sdk/subagents) · [permissions](https://code.claude.com/docs/en/agent-sdk/permissions) · [secure-deployment](https://code.claude.com/docs/en/agent-sdk/secure-deployment) · [python ref](https://code.claude.com/docs/en/agent-sdk/python). Managed Agents: [overview](https://platform.claude.com/docs/en/managed-agents/overview) · [sessions](https://platform.claude.com/docs/en/managed-agents/sessions) · [environments](https://platform.claude.com/docs/en/managed-agents/environments) · [self-hosted](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes) · [multi-agent](https://platform.claude.com/docs/en/managed-agents/multi-agent) · [permission-policies](https://platform.claude.com/docs/en/managed-agents/permission-policies) · [pricing](https://platform.claude.com/docs/en/about-claude/pricing). Engineering: [building agents](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk) · [managed agents](https://www.anthropic.com/engineering/managed-agents) · [sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing). Prior art: [SWE-agent](https://arxiv.org/abs/2405.15793) · [OpenHands](https://arxiv.org/abs/2407.16741) · [MetaGPT](https://arxiv.org/abs/2308.00352) · [GPT-Pilot](https://github.com/Pythagora-io/gpt-pilot) · [Devin 2025 review](https://cognition.com/blog/devin-annual-performance-review-2025) · [Replit agent (LangChain)](https://www.langchain.com/breakoutagents/replit). Eval: [pass@k](https://www.emergentmind.com/topics/pass-k-metrics-2508a3b6-8dc0-488f-a854-891fb35d80b0) · [LLM-judge stability](https://arxiv.org/html/2508.02994v1) · [SSIM visual](https://wopee.io/blog/screenshot-comparison-algorithms-visual-testing/).
