# M9 Brownfield Adopt — point dev-agent at a repo it didn't build

**Status:** first slice implemented. Editing adopted code and fitting existing CI remain open.
**Decision trail:** the job is understand and design first, with change execution as a later
step. The proof level is static analysis plus a machine-checked rebuild of every service a
recipe fits. There is no live boot or contract probing in this slice.

## 1. Problem

`update-system` (M25) can only change systems dev-agent built, because it loads the prior
gated design (`<run_dir>/design.json` and `services/`). An existing codebase has no such
artifacts. Adopt closes that gap: given one or more existing repos, it produces the reviewed,
build-proven design artifacts the update path already consumes, so
`update-system <adopt-run-dir> <change.md>` needs no new plumbing.

## 2. Shape

```
devagent adopt <repo-path>... [--out DIR] [--skip-map] [--title T]
```

- Never writes into the source repo. A run dir inside a source repo, a source repo inside the
  run dir, and a run dir that already holds files are refused.
- Output is a synthetic run dir shaped like a `build-system` run: `design.json`,
  `services/<name>/out` (a copy of the service's source), an append-only `ledger.jsonl` and
  `report.html`, plus `survey.json`.
- Four stages: survey, map, assemble, prove. A failed service degrades honestly (marked in the
  report); only an unreadable repo aborts the run.
- Several repos can be adopted as one system. A frontend that calls a backend in another repo
  cannot be wired if adopted alone, so the surveys are merged before assembly. Two services
  with the same name are refused.
- A service name is one path component matching `[A-Za-z0-9][A-Za-z0-9._-]*`. Symlinks in the
  source are copied as links and never followed.
- Exit codes: 0 adopted, 1 gate failed or no services found, 2 bad input or an egress proxy
  that could not start.

## 3. Stages

### 3.1 Survey (deterministic, no model)

A pure-Python repo walk. It detects service roots (Maven `pom.xml`, multi-module aware;
`package.json` with build scripts; static sites), classifies each against the recipe registry,
and records libraries, package mirrors the build must reach, and explicit **recipe gaps**
("service X needs a recipe for Y"). The gap list is the evidence for which recipes are still
missing. Fully unit-testable against fixture trees.

### 3.2 Map (agentic, per service)

One contained session per service: sandbox container, repo mounted **read-only**, egress
allowlist as for builds, reading tools only. The session reads the code and returns a
description and the API contract the service provides. The host validates the answer with
pydantic and re-asks on a validation error, capped at 2 retries. A fragment that does not
validate is never repaired on the host.

Mapping streams source code to a model API, so the arm is chosen on purpose. The default is
Claude. `DEVAGENT_ADOPT_ARM=deepseek` selects the other arm, and an arm whose credential is
missing refuses to run. `--skip-map` skips this stage: services are described from survey
evidence and marked NOT MAPPED.

### 3.3 Assemble + gate (deterministic)

Fragments are composed into a `SystemDesign` by deterministic Python. This deviates from the
original plan, which had a model call compose the design: ids and consumes/provides wiring
follow mechanically from the survey and the fragments, so a model adds nothing a gate would
not have to re-check. The existing pydantic wiring validators gate structure. Two further gates:
every surveyed service appears in the design, and every contract has a producer. A contract the
system consumes but does not own is recorded as an external dependency and shown in the report.

### 3.4 Prove (deterministic build check)

Each service's source is copied into the run dir. A service with a matched recipe is rebuilt
in the sandbox with the recipe's `build_cmd`, and `artifact_glob` is asserted. A Maven module
builds from its reactor root with `-pl <module> -am`. The result per service is **proven**,
**unproven** or **unmappable**; a partially proven adoption is still a reviewable artifact. A
failed build's log is kept beside the service. There is no boot, no datastore and no
acceptance probe.

## 4. Recipes

Recipes are entries in the M11 recipe registry.

- `maven-spring-boot-jdk{8,11,17,21,25}`: one per Java LTS version. The survey reads the Java
  version the POM declares and selects the matching recipe; an undeclared, unresolvable or
  unsupported version is a recipe gap. There is no single pinned JDK. The build is `mvn -pl <module> -am package` and
  the artifact is `target/*.jar`.
- `node-vite-yarn`, `node-vite-npm`: Vite frontends by lockfile. A Vite app with a pnpm lockfile
  matches the existing `node-vite-react` recipe.
- `static-web`: a site with no framework and no package manager.
- Anything else is a recipe gap and the service is `unmappable`.
- Datastore recipes are added only when a build needs one; booting is a later milestone.

## 5. Human review gate

Adoption ends at a human gate: the operator reviews `report.html` and `design.json` before the
artifacts are treated as truth. The report makes what is not known as visible as what is:
unmapped services, recipe gaps, failed rebuilds and external dependencies. Nothing downstream
consumes an unreviewed adoption.

## 6. Testing

TDD throughout. Unit: survey detection on fixture repo trees; assemble gates on hand-built
fragments; recipe manifests parse and resolve; the mapper's containment and validation loop;
the CLI. Integration: adopt a small fixture repo end to end with the map stage faked. None
needs Docker, the network or a model. A live mapping session and a live rebuild are run by
hand.

## 7. Non-goals

- Live boot, contract probing or acceptance checks against adopted services.
- Editing adopted code. The follow-on is `update-system` on the adopt run dir, which is not
  supported for Maven services yet because the engine's rebuild step does not fill the
  `{module}` placeholder.
- Flutter/Dart and Python-pipeline recipes. Adopt surveys them; proving waits for demand.
- Adopting a whole portfolio at once; one reviewed adoption first.
