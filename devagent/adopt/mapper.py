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
answer that contains it is discarded, never kept or fed back. "Contains" means the key itself,
or any run of `CREDENTIAL_RUN` of its characters, once whitespace and zero-width characters are
removed from the answer (a shorter key is looked for whole). An encoded or reversed key is not
detectable this way.
"""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .. import egress
from ..executor_deepseek import ANTHROPIC_COMPAT_URL, DEFAULT_MODEL, KEY_ENV as DEEPSEEK_KEY_ENV
from .assemble import Fragment
from .survey import SAFE_NAME

RUNNER = Path(__file__).parent / "mapper_runner.py"
DEFAULT_IMAGE = os.getenv("DEVAGENT_M2_IMAGE", "devagent-sandbox:m2")
ARMS = ("claude", "deepseek")
_MAX_RETRIES = 2
#: Long enough that ordinary text does not share a run with a random key by chance.
CREDENTIAL_RUN = 20
_INVISIBLE = re.compile(r"[\s\u200b\u200c\u200d\u2060\ufeff]+")


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


def _container_argv(image: str, repo: Path, service_path: str, network, proxy_url,
                    name: str = "", siblings=()) -> list[str]:
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
            image, "python3", "/mapper.py", f"--service={service_path}", f"--name={name}",
            f"--siblings={','.join(siblings)}"]


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


#: Raised instead of an answer or an error text that carried the credential, which is never
#: kept, shown or fed back.
_DISCARDED = "the mapping session's answer carried the credential and was discarded"
_WITHHELD = "the mapping session failed; its error text carried the credential and is withheld"


def map_service(repo: Path, service, *, siblings=(), image: str = DEFAULT_IMAGE, network=None,
                proxy_url=None, timeout: int = 900, runner=None) -> Fragment | None:
    """Map one service. `siblings` are the names of the other services in the system, which the
    session may name in depends_on.

    Raises RuntimeError when the session gives no usable answer (none at all, its own reported
    failure, or one carrying the credential), with the reason and never the credential, so the
    lane can record why. Returns None when the answer is still invalid after the retries, or the
    service's path or name is not one to put in a container argv; the lane then describes the
    service from survey evidence and marks it NOT MAPPED. Names and the path come from repo
    files, so they are checked before they reach the argv."""
    if not _inside_repo(service.path) or not SAFE_NAME.fullmatch(service.name or ""):
        return None
    siblings = [n for n in siblings
                if isinstance(n, str) and SAFE_NAME.fullmatch(n) and n != service.name]
    run = runner or _run_session
    secret = _credential()
    last_error = None
    for attempt in range(1 + _MAX_RETRIES):
        raw = run(image, repo, service.path, network, proxy_url, timeout, last_error,
                  service.name, siblings)
        # None of these is worth asking again, and an answer carrying the credential must not
        # be kept.
        if not raw:
            raise RuntimeError("the mapping session printed no answer")
        failure = _session_error(raw)
        if failure is not None:
            raise RuntimeError(_WITHHELD if _carries(raw, secret) else failure)
        if _carries(raw, secret):
            raise RuntimeError(_DISCARDED)
        try:
            out = FragmentOut.model_validate_json(raw)
        except ValidationError as e:
            # Feed the failure back and re-ask; never repair it here.
            last_error = _retry_context(e)
            if attempt == _MAX_RETRIES or _carries(last_error, secret):
                return None
            continue
        if _carries(out.model_dump_json(), secret):
            raise RuntimeError(_DISCARDED)  # the same check on the validated answer
        return Fragment(
            service=service.name, prd_slice=out.summary, kind=out.kind,
            provides=tuple(c.model_dump() for c in out.provides),
            consumes=tuple(out.consumes), depends_on=tuple(out.depends_on))
    return None


def _strings(value) -> str:
    """Every string in a decoded JSON value, keys included, joined."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "".join(_strings(k) + _strings(v) for k, v in value.items())
    if isinstance(value, list):
        return "".join(_strings(v) for v in value)
    return ""


def _carries(text: str, secret: str | None) -> bool:
    """Does `text` hold the credential or a `CREDENTIAL_RUN`-character run of it, once whitespace
    and zero-width characters are removed? A JSON text is also judged by its decoded strings,
    so an escape sequence does not hide what it encodes."""
    if not secret:
        return False
    texts = [text]
    try:
        texts.append(_strings(json.loads(text)))
    except ValueError:
        pass
    n = min(CREDENTIAL_RUN, len(secret))
    runs = {secret[i:i + n] for i in range(len(secret) - n + 1)}
    return any(run in flat for flat in (_INVISIBLE.sub("", t) for t in texts) for run in runs)


def _session_error(raw: str) -> str | None:
    """The runner reports a failed session as a JSON object with an "error" key; its text, or
    None for any other answer."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or "error" not in obj:
        return None
    return str(obj["error"])[:1000] or "the mapping session failed without an error text"


def _retry_context(e: ValidationError) -> str:
    """What failed, from each error's location and message only. The input values are left
    out, since they are the model's own output and could carry anything it read."""
    lines = []
    for err in e.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "answer"
        lines.append(f"{loc}: {err.get('msg', '')}")
    return "\n".join(lines)[:1500]


def _run_session(image, repo, service_path, network, proxy_url, timeout, prior_error, name,
                 siblings):
    argv = _container_argv(image, Path(repo), service_path, network, proxy_url, name, siblings)
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
    """A `map_fn(repo, service, siblings=())` for the lane, bound to this run's containment
    settings. The lane passes `siblings`, the names of the other surveyed services.

    The arm and its credential are checked here, so a misconfigured run fails before any
    service is mapped instead of marking every service NOT MAPPED. Raises ValueError for an
    unknown arm and RuntimeError for a missing credential."""
    _subprocess_env()                   # the same checks the per-service path runs; env dropped
    def mapper(repo, service, siblings=()):
        return map_service(repo, service, siblings=siblings, image=image, network=network,
                           proxy_url=proxy_url, timeout=timeout)
    return mapper
