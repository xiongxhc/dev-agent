# M9 Brownfield Adopt — point dev-agent at a repo it didn't build

**Status:** design approved in session, 2026-08-19. First slice of the roadmap's open M9
("brownfield: operate inside existing repos, detect stack"); the M9 tail — fitting existing
CI, editing adopted code — builds on this slice's artifacts.
**Decision trail:** target = internal Estidama estate → job = understand + design first
(change execution is milestone 2) → proof level = static analysis **plus a machine-checked
rebuild** of every mappable service (no live boot/contract probing in M1).

## 1. Problem

`update-system` (M25) can only change systems dev-agent built, because it loads the prior
gated design (`<run_dir>/design.json + services/`). The internal estate (~24 projects,
dominantly Java 8 Spring Boot + React/TypeScript, plus Flutter and Python pipelines) has no
such artifacts. Adopt closes that gap: given an existing repo, produce the **reviewed,
build-proven** design artifacts the update path already consumes — making milestone 2
(`update-system <adopt-run-dir> <change.md>`) a zero-plumbing follow-on.

## 2. Shape

New CLI lane:

```
devagent adopt <repo-path> [--out runs/adopt-<id>]
```

- Never writes into the source repo. Output is a **synthetic run dir** shaped exactly like a
  `build-system` run: `design.json`, `services/<name>/`, append-only ledger, `report.html`.
- Four stages, each a gate in the existing orchestrator idiom: survey → map → assemble →
  prove. A failed service degrades honestly (marked in the report); only an unreadable repo
  aborts the run.

## 3. Stages

### 3.1 Survey (deterministic, no LLM)

Pure-Python repo walk. Detect service roots: Maven `pom.xml` (multi-module aware),
`package.json` with build scripts, Dockerfiles, docker-compose. Classify each root against
the recipe registry; emit `survey.json` including explicit **recipe gaps** ("service X is
maven-spring-boot: no recipe"). Fully unit-testable against fixture trees. The pilot's
survey — not guesswork across 24 projects — finalizes the new-recipe list.

### 3.2 Map (agentic, per service)

One contained session per service: sandbox container, repo mounted **read-only**, egress
allowlist as today, **no write tools**. The session reads the code and emits (a) a scope
fragment and (b) the service's *provided* API contract (OpenAPI derived from its
controllers/routes) as JSON. Host-side pydantic validation with the `llm.py`-style
retry-on-ValidationError loop (cap 2). Runner mirrors `sdk_runner.py` with a mapping prompt.

**Governance:** mapping streams internal source code to a model API. Default arm for the
mapper is **Claude (sdk)**; the deepseek arm is a deliberate per-run opt-in
(`DEVAGENT_ADOPT_ARM=deepseek`), never a silent default — cost does not outrank data
governance for the internal estate.

### 3.3 Assemble + gate (brain)

The Claude brain composes per-service fragments into a full `SystemDesign` (ids,
consumes/provides wiring). Existing pydantic wiring validators gate structure for free. Two
new deterministic gates: every surveyed service appears in the design; every contract has a
producer.

### 3.4 Prove (deterministic build check)

Per service with a matched recipe: rebuild in the sandbox via the recipe's `build_cmd`,
assert `artifact_glob` — reusing the verifier's rebuild machinery. Result per service is
**proven / unproven / unmappable**; a partially-proven adoption is still a reviewable
artifact. No boot, no datastores, no acceptance probes in M1.

## 4. New recipes (pilot-driven, M11 manifests — not code)

- `maven-spring-boot`: JDK-8-compatible toolchain image (the WAJIB-class parent-POM ceiling
  is real across the estate), `mvn -pl … package`-style build_cmd, `target/*.jar` artifact.
- One frontend toolchain per what the pilot's survey reveals (CRA/Umi/webpack likely — the
  estate is not Vite-shaped).
- Datastore recipes only when a *build* needs them (builds don't; booting is milestone ≥2).

## 5. Pilot

**OrgChart** (one Spring Boot service + one React frontend — the smallest real shape).
Survey may veto; fall back to the next-smallest project. The live pilot run is the
milestone's acceptance test.

## 6. Human review gate

Adoption ends at a human gate: the operator reviews `report.html` + `design.json` before
the artifacts are treated as truth. Nothing downstream (milestone 2) consumes an unreviewed
adoption.

## 7. Testing

TDD throughout. Unit: survey detection on fixture repo trees; assemble gates on hand-built
fragments; recipe manifests parse and resolve. Integration: adopt a tiny in-repo fixture
repo end-to-end with the map stage faked. Live: the pilot run (real tokens, real Docker).

## 8. Non-goals (M1)

- Live boot / contract probing / acceptance checks against adopted services.
- Editing adopted code (milestone 2: `update-system` on the adopt run dir).
- Flutter/Dart and Python-pipeline recipes (adopt surveys them; proving waits for demand).
- Auto-adoption of all 24 projects; one reviewed pilot first.
