"""Map — one contained session per service that reads it; emits a scope fragment and a contract.

Three properties are deliberate and each is a containment decision, not a style choice:

* **Reading inside the repo only.** The repo is mounted `:ro`, and the session is locked to the
  Read, Grep and Glob tools on paths inside the mount (the three locks are described in
  mapper_runner.py). A model with tools must not be able to change the code it is describing.
* **Host-side validation.** The session's answer is parsed and validated with pydantic on the
  host and re-asked on a ValidationError, with the failing fields and the validator's messages
  fed back. A fragment that does not validate is never repaired here: a hand-repaired answer is
  no longer what the model read.
* **Claude by default.** Mapping sends the repo's source code to a model API, so the operator
  chooses which API on purpose: the arm is Claude unless `DEVAGENT_ADOPT_ARM` names another
  one, and an arm whose credential is missing refuses to run.

Credentials follow the executors' rule: the `docker run` argv names each secret variable
(`-e ANTHROPIC_API_KEY`) and the value travels only in the subprocess environment, so it never
appears in the argv, which callers may log. The session's own process holds the value, so an
answer that contains it is discarded, never kept or fed back.
"""

import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .. import egress
from ..executor_deepseek import ANTHROPIC_COMPAT_URL, DEFAULT_MODEL, KEY_ENV as DEEPSEEK_KEY_ENV
from .assemble import Fragment

RUNNER = Path(__file__).parent / "mapper_runner.py"
DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
ARMS = ("claude", "deepseek")
_MAX_RETRIES = 2


class ContractOut(BaseModel):
    id: str = Field(..., min_length=1)
    kind: Literal["openapi", "db_schema", "auth_token", "event"] = "openapi"
    spec: dict = Field(default_factory=dict)


class FragmentOut(BaseModel):
    """What one mapping session must return. Kept small on purpose: a description and the
    contracts the service provides, not a full design; assemble composes the design."""
    summary: str = Field(..., min_length=20, max_length=4000)
    kind: Literal["frontend", "backend", "datastore"] | None = None
    provides: list[ContractOut] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self):
        """Contradictions inside one answer are refused here, so they are re-asked for this
        service instead of failing the assembled design for every service."""
        ids = [c.id for c in self.provides]
        if len(ids) != len(set(ids)):
            raise ValueError("provides lists the same contract id more than once")
        if set(ids) & set(self.consumes):
            raise ValueError("consumes names a contract this service itself provides")
        return self


def adopt_arm(env=None) -> str:
    """Which model endpoint the mapper uses: Claude unless `DEVAGENT_ADOPT_ARM` names another."""
    env = os.environ if env is None else env
    arm = env.get("DEVAGENT_ADOPT_ARM") or "claude"
    if arm not in ARMS:
        raise ValueError(f"unknown adopt arm {arm!r} (known: {', '.join(ARMS)})")
    return arm


def _container_argv(image: str, repo: Path, service_path: str, network, proxy_url) -> list[str]:
    arm_flags = []
    if adopt_arm() == "deepseek":
        # The base URL and model are not secrets: inline so the argv shows which API was used.
        arm_flags = ["-e", f"ANTHROPIC_BASE_URL={ANTHROPIC_COMPAT_URL}",
                     "-e", "ANTHROPIC_AUTH_TOKEN",          # value via env, NOT argv
                     "-e", f"ANTHROPIC_MODEL={DEFAULT_MODEL}"]
    return ["docker", "run", "--rm",
            *egress.docker_flags(network, proxy_url),
            "-e", "ANTHROPIC_API_KEY",                      # value via env, NOT argv
            *arm_flags,
            "-e", "HOME=/home/node",
            "--user", "1000:1000",
            # :ro, so nothing in the container can change the repo it is describing.
            "-v", f"{repo}:/src:ro",
            "-v", f"{RUNNER}:/mapper.py:ro",
            # One `--opt=value` word, so a path starting with "-" is still a value.
            image, "python3", "/mapper.py", f"--service={service_path}"]


def _key_env(arm: str) -> str:
    return DEEPSEEK_KEY_ENV if arm == "deepseek" else "ANTHROPIC_API_KEY"


def _credential() -> str | None:
    """The value the session will hold, if it is set; used only to recognise it in output."""
    try:
        return os.environ.get(_key_env(adopt_arm())) or None
    except ValueError:
        return None


def _subprocess_env() -> dict:
    """Env for the `docker run` subprocess, where the by-name `-e` flags pick their values."""
    arm = adopt_arm()
    var = _key_env(arm)
    key = os.environ.get(var)
    if not key:
        raise RuntimeError(
            f"the adopt mapper's {arm} arm needs {var} set. Mapping streams the repo's source "
            f"code to a model API, so it is never run against an unconfigured endpoint.")
    if arm == "deepseek":
        return {**os.environ, "ANTHROPIC_API_KEY": key, "ANTHROPIC_AUTH_TOKEN": key}
    return {**os.environ}


def _inside_repo(path: str) -> bool:
    """A service path is mapped only as a relative path with no `..` component: it comes from
    repo files and names a directory under the read-only mount."""
    p = PurePosixPath(path)
    return not p.is_absolute() and ".." not in p.parts


def map_service(repo: Path, service, *, image: str = DEFAULT_IMAGE, network=None,
                proxy_url=None, timeout: int = 900, runner=None) -> Fragment | None:
    """Map one service. Returns None when the session produced nothing usable, or the service's
    path points outside the repo; the lane then describes the service from survey evidence and
    marks it NOT MAPPED."""
    if not _inside_repo(service.path):
        return None
    run = runner or _run_session
    secret = _credential()
    last_error = None
    for attempt in range(1 + _MAX_RETRIES):
        raw = run(image, repo, service.path, network, proxy_url, timeout, last_error)
        if not raw or (secret and secret in raw) or _session_failed(raw):
            # No answer, an answer carrying the credential, or a session that reported its own
            # failure: none of these is worth asking again, and the first must not be kept.
            return None
        try:
            out = FragmentOut.model_validate_json(raw)
        except ValidationError as e:
            # Feed the failure back and re-ask; never repair it here.
            last_error = _retry_context(e)
            if attempt == _MAX_RETRIES or (secret and secret in last_error):
                return None
            continue
        if secret and secret in out.model_dump_json():
            return None                 # the same check on the decoded answer (JSON escapes)
        return Fragment(
            service=service.name, prd_slice=out.summary, kind=out.kind,
            provides=tuple(c.model_dump() for c in out.provides),
            consumes=tuple(out.consumes), depends_on=tuple(out.depends_on))
    return None


def _session_failed(raw: str) -> bool:
    """The runner reports a failed session as a JSON object with an "error" key."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return False
    return isinstance(obj, dict) and "error" in obj


def _retry_context(e: ValidationError) -> str:
    """What failed, from each error's location and message only. The input values are left
    out, since they are the model's own output and could carry anything it read."""
    lines = []
    for err in e.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "answer"
        lines.append(f"{loc}: {err.get('msg', '')}")
    return "\n".join(lines)[:1500]


def _run_session(image, repo, service_path, network, proxy_url, timeout, prior_error):
    argv = _container_argv(image, Path(repo), service_path, network, proxy_url)
    if prior_error:
        argv.append(f"--retry-context={prior_error}")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                         env=_subprocess_env())
    # The runner prints exactly one JSON object on stdout; anything else is a failed session.
    for line in reversed((res.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return line
    return None


def make_mapper(*, image: str = DEFAULT_IMAGE, network=None, proxy_url=None, timeout: int = 900):
    """A `map_fn(repo, service)` for the lane, bound to this run's containment settings."""
    def mapper(repo, service):
        return map_service(repo, service, image=image, network=network, proxy_url=proxy_url,
                           timeout=timeout)
    return mapper
