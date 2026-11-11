# M9 Brownfield Adopt — point dev-agent at a repo it didn't build

**Status:** first slice implemented. Editing adopted code and fitting existing CI remain open.
**Decision trail:** the job is understand and design first, with change execution as a later
step. The proof level is static analysis plus a machine-checked rebuild of every service a
recipe fits. There is no live boot or contract probing in this slice.

## 1. Problem

`update-system` (M25) can only change systems dev-agent built, because it loads the prior
gated design (`<run_dir>/design.json` and `services/`). An existing codebase has no such
artifacts. Adopt produces them: given one or more existing repos, it writes a reviewed,
build-proven design in the run-dir shape the update path loads. Running `update-system` on an
adopt run dir is not supported yet (§7).

## 2. Shape

```
devagent adopt <repo-path>... [--out DIR] [--skip-map] [--title T]
```

- Never writes into the source repo. A run dir inside a source repo, a source repo inside the
  run dir, and a run dir that already holds files are refused.
- Output is a synthetic run dir shaped like a `build-system` run: `design.json`,
  `services/<name>/out` (a copy of the service's source), an append-only `ledger.jsonl` and
  `report.html`, plus `survey.json`.
- Four stages: survey, map, assemble, prove. A service that fails to map or build is marked in
  the report and the run continues. A run ends before prove in three cases: a repo that cannot
  be read, a repo with no services (`survey_only`), and a failed design gate (`gate_failed`).
- Several repos can be adopted as one system. A frontend that calls a backend in another repo
  cannot be wired if adopted alone, so the surveys are merged before assembly. Two services
  with the same id are refused; a service's id is its name in lower case with `_` read as `-`.
- A service name is one path component matching `[A-Za-z0-9][A-Za-z0-9._-]*`. Symlinks in the
  source are copied as links and never followed. A linked directory that leads outside the repo
  is not surveyed as a service.
- Refusals come first and cost nothing: a path that is not a directory, an unusable run dir, and
  an unknown mapping arm or missing credential all exit before anything is created and before
  the shared egress proxy is touched.
- Exit codes: 0 adopted, 1 gate failed or no services found, 2 bad input (including a repo
  that cannot be read or a run dir that cannot be written), a bad mapping setup, or, with egress
  on, Docker missing or an egress proxy that cannot be started or shared.

## 3. Stages

### 3.1 Survey (deterministic, no model)

A pure-Python repo walk. It detects service roots (Maven `pom.xml`, multi-module aware;
`package.json` with build scripts). Maven modules are read from `<modules>`; a module that is
itself an aggregator is followed into its own modules, and a module listed only in a profile is
noted and not adopted. A node package with no `build` or `build:*` script is not a service. A
repo whose root holds an `index.html`, and which has no Maven or node service and no library, is
one static site, unless a `package.json` beside it declares a framework or workspaces: a tool
builds that page, and the static recipe only copies files. Each root is classified against the
recipe registry. The survey also records libraries, package mirrors the build must reach,
and explicit **recipe gaps** ("service X needs a recipe for Y"). The gap list is the
evidence for which recipes are still missing. Fully unit-testable against fixture trees.

### 3.2 Map (agentic, per service)

One contained session per service: sandbox container, repo mounted **read-only**, egress
allowlist as for builds. The session reads with Read, Grep and Glob, and three locks confine
it: the documented built-in tools that do not read, and every MCP tool, are removed from the
session; a call that would need approval and is not pre-approved is denied; and a hook rejects
any other tool and any read whose target resolves outside the repo. The session reads the code
and returns a
description and the API contract the service provides. The host validates the answer with
pydantic and re-asks on a validation error, capped at 2 retries. A fragment that does not
validate is never repaired on the host.

Mapping streams source code to a model API, so the arm is chosen on purpose. The default is
Claude. `DEVAGENT_ADOPT_ARM=deepseek` selects the other arm. An unknown arm, or an arm whose
credential is missing, is refused when the mapper is created, before any service is mapped.
`--skip-map` skips this stage: services are described from survey
evidence and marked NOT MAPPED.

### 3.3 Assemble + gate (deterministic)

Fragments are composed into a `SystemDesign` by deterministic Python, not a model call:
ids and consumes/provides wiring follow mechanically from the survey and the fragments, so a
model would add nothing a gate does not have to re-check. The existing pydantic wiring
validators gate structure. Two further gates:
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
  unsupported version is a recipe gap. Each recipe pins its own JDK. The build is
  `mvn -pl <module> -am package` and the artifact is `{module}/target/*.jar`.
- `node-vite-yarn`, `node-vite-npm`: Vite frontends by lockfile. A Vite app with a pnpm lockfile
  matches the existing `node-vite-react` recipe.
- `static-web`: a site with a root `index.html`, no Maven or node service and no library.
- A node app with no lockfile is a recipe gap: no install can be frozen, so a rebuild would
  prove nothing. Anything else is a recipe gap too, and the service is `unmappable`.
- Datastore recipes are added only when a build needs one; booting is a later milestone.

## 5. Human review gate

Adoption ends at a human gate: the operator reviews `report.html` and `design.json` before the
artifacts are treated as truth. The report lists unmapped services, recipe gaps, failed
rebuilds and external dependencies. Nothing in the code enforces the review; it is the
operator's step.

## 6. Testing

Unit: survey detection on fixture repo trees; assemble gates on hand-built
fragments; recipe manifests parse and resolve; the mapper's containment and validation loop;
the CLI. Integration: adopt a small fixture repo end to end with the map stage faked. The
automated suite needs no Docker, network or model, so it does not include a live mapping session
or a live rebuild.

## 7. Non-goals

- Live boot, contract probing or acceptance checks against adopted services.
- Editing adopted code. The follow-on is `update-system` on the adopt run dir, which is not
  supported yet for any service kind: the update architect is not shown the adopt recipes, the
  rebuild step uses its default image and does not fill the `{module}` placeholder, and recipe
  registry hosts are not allowlisted on that path.
- Flutter/Dart and Python projects. The survey does not detect them as services.
- Batch adoption of many unrelated systems in one run. Repos given to one run form one system.
