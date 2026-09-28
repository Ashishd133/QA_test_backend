"""B2.7-07 sandbox child process entrypoint -- launched only as
`python -I -S -m app.engine.metrics.sandbox_worker` by
app/engine/metrics/sandbox.py, never invoked any other way and never
imported for its functions. Reads a `{"code": str, "context": dict}` JSON
request from stdin, executes `code` against a restricted namespace with an
audit hook and OS resource limits both active, and writes exactly one JSON
verdict object to stdout. See sandbox.py's module docstring for the full
two-layer threat model this and the parent process together implement.

Contract for `code`: it must set a top-level variable named `result`, a
dict with `status` ('passed'|'failed'|'warn'), `value` (JSON-serializable),
and `rationale` (str). Anything else -- missing `result`, wrong shape, a
raised exception, a denied operation, a timeout -- becomes
`{"ok": false, "error_code": ..., "rationale": ...}` instead. There is no
path that returns `"ok": true` for code that didn't finish executing
cleanly inside every limit below.

Classification is done by explicit wrapper functions (`_denied_open`,
`_restricted_import`), not by leaving `open`/`import` out of the builtins
entirely: the latter would just produce a generic `NameError` for every
denied operation, indistinguishable from any other bug -- the ticket's own
bar is a *distinct* error code per attempt type (network vs filesystem vs
subprocess), which requires actually intercepting the call, not omitting
it. `sys.addaudithook` stays installed as a second, independent layer in
case anything reaches a real `open`/`socket`/`fork` call some other way.

**Fixed after security review (2026-09-28)**: the first version of this
module put the real `re`/`statistics` module objects directly into the
exec namespace. Both are pure-Python stdlib modules, so any of their
functions expose `.__globals__` -- the function's home module dict, which
CPython auto-populates with a `__builtins__` key pointing at the real,
*unrestricted* builtins. One line of sandboxed code
(`re.sub.__globals__['__builtins__'].__import__('os').system(...)`)
recovered full builtins with no subclasses-walking needed, bypassing
`_denied_open`/`_restricted_import` entirely. `_reject_dunder_access()`
below closes this by rejecting any `__dunder__`-shaped attribute or name
at *parse* time, before `exec()` ever runs, regardless of which object it
targets -- not specific to `re`/`statistics`, and not defeated by
`getattr()` with a dynamically-built string since `getattr` was never in
the allowed builtins to begin with.

**Residual limitation, not fully closed**: a sufficiently creative escape
that reaches an unrestricted callable *without* writing a literal
`__dunder__` token (there is no fully general defense against this short
of a real AST allowlist compiler, e.g. RestrictedPython) is still
conceivable in principle. The OS-level layer (sandbox.py's empty env, low
rlimits, own process group, parent-side wall-clock timeout) plus the
audit hook (global and un-disableable from Python -- `sys.addaudithook`
has no removal API) are what this design actually relies on as the
backstop if interpreter-level restriction is ever bypassed some other
way: even then, a real socket call still hits the audit hook and an
exhausted `RLIMIT_NOFILE`, a real `open()` still hits the audit hook, and
`os.system`/`os.popen`/`os.exec*`/`os.posix_spawn`/`subprocess.Popen` are
all explicitly in the hook's denylist (not just `os.fork`). Flagged here
explicitly for the security review this ticket calls for, not left
implicit.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import io
import json
import math
import re
import signal
import statistics
import sys
from typing import Any

# Every module/name the restricted namespace allows is imported here,
# above `_apply_resource_limits()` -- a low RLIMIT_NOFILE/RLIMIT_NPROC
# applied before the interpreter finishes its own startup imports could
# break Python itself, not just the untrusted code (this ticket's own
# documented risk, not a hypothetical).
_ALLOWED_BUILTIN_NAMES = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "range",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "True",
        "False",
        "None",
        "isinstance",
        "type",
        "print",  # safe: stdout is redirected to a discarded buffer during exec()
        # Common exception types -- a metric author raising/catching these
        # is normal control flow, not a security concern; the ones left
        # out (OSError, ImportError, and friends) are either unreachable
        # anyway (nothing that could raise them is in scope) or would
        # leak information about the sandbox's own denial mechanism.
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "ZeroDivisionError",
        "StopIteration",
        "RuntimeError",
    }
)
_ALLOWED_MODULES: dict[str, object] = {"math": math, "re": re, "statistics": statistics}

# Best-effort classification of a denied `import X` by module name -- an
# unrecognized module name defaults to "filesystem_denied" (the most
# common thing a metric author would actually be reaching for beyond the
# allowlist) rather than silently defaulting to a misleading category.
_MODULE_DENY_CLASS: dict[str, str] = {
    "socket": "network_denied",
    "ssl": "network_denied",
    "urllib": "network_denied",
    "http": "network_denied",
    "requests": "network_denied",
    "asyncio": "network_denied",
    "select": "network_denied",
    "os": "filesystem_denied",
    "pathlib": "filesystem_denied",
    "shutil": "filesystem_denied",
    "io": "filesystem_denied",
    "tempfile": "filesystem_denied",
    "glob": "filesystem_denied",
    "subprocess": "subprocess_denied",
    "multiprocessing": "subprocess_denied",
    "threading": "subprocess_denied",
    "ctypes": "subprocess_denied",
    "importlib": "subprocess_denied",
}

_CPU_SOFT_SECONDS = 1
_CPU_HARD_SECONDS = 2
_MEMORY_BYTES = 256 * 1024 * 1024
_MAX_OPEN_FILES = 4


class _SandboxDenied(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _CPUExceeded(Exception):
    pass


def _install_audit_hook() -> None:
    """The classifier: OS resource limits alone can't tell a network
    attempt from a filesystem one (both allocate a file descriptor and
    both fail the same way against a low RLIMIT_NOFILE) -- this is what
    actually produces the distinct error codes the ticket requires."""

    def hook(event: str, _args: object) -> None:
        if event.startswith("socket."):
            raise _SandboxDenied("network_denied", f"blocked: {event}")
        if event in ("open", "os.open", "io.open"):
            raise _SandboxDenied("filesystem_denied", f"blocked: {event}")
        if event in (
            "os.fork",
            "os.posix_spawn",
            "os.spawn",
            "os.system",
            "os.exec",
            "subprocess.Popen",
            "ctypes.dlopen",
            "ctypes.dlsym",
        ):
            raise _SandboxDenied("subprocess_denied", f"blocked: {event}")

    sys.addaudithook(hook)


def _reject_dunder_access(code: str) -> None:
    """Rejects any `__dunder__`-shaped name or attribute access before
    `exec()` ever runs -- closes the `re.sub.__globals__['__builtins__']`
    class of escape found in security review, which needs no subclasses-
    walking and isn't specific to `re`/`statistics`: any pre-existing
    pure-Python callable exposes `__globals__` the same way. Not a full
    AST allowlist compiler (see module docstring's residual-limitation
    note) -- just the specific, cheap, high-value check that closes what
    was actually found."""
    try:
        tree = ast.parse(code, filename="<metric>", mode="exec")
    except SyntaxError as e:
        raise _SandboxDenied("invalid_output", f"metric code does not parse: {e}") from e
    for node in ast.walk(tree):
        name: str | None = None
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr.endswith("__")
        ):
            name = node.attr
        elif isinstance(node, ast.Name) and node.id.startswith("__") and node.id.endswith("__"):
            name = node.id
        if name is not None:
            raise _SandboxDenied(
                "forbidden_syntax", f"dunder name/attribute {name!r} is not allowed in metric code"
            )


def _apply_resource_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SOFT_SECONDS, _CPU_HARD_SECONDS))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_BYTES, _MEMORY_BYTES))
    except ValueError:
        # Confirmed empirically, not just "weakly enforced": macOS's
        # setrlimit rejects RLIMIT_AS outright ("current limit exceeds
        # maximum limit", even lowering both soft and hard together) --
        # this is a real platform gap, not a bug here. Linux (Railway,
        # prod) accepts it; local dev on macOS runs every other limit
        # (CPU/NOFILE/NPROC) plus the audit hook and empty-env subprocess
        # isolation without the memory cap. A memory-bomb metric is only
        # actually bounded on Linux -- see tests/test_metric_sandbox.py's
        # darwin skip on that specific test.
        pass
    resource.setrlimit(resource.RLIMIT_NOFILE, (_MAX_OPEN_FILES, _MAX_OPEN_FILES))
    # Blocks forking/spawning from within -- defense in depth alongside the
    # audit hook's os.fork/os.posix_spawn interception, not a substitute
    # for it: some platforms don't enforce RLIMIT_NPROC for a process
    # running as root (containers commonly do), so this must never be the
    # only thing standing between user code and a spawned subprocess.
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))


def _denied_open(*_args: object, **_kwargs: object) -> None:
    raise _SandboxDenied("filesystem_denied", "open() is not available to sandboxed metrics")


def _restricted_import(name: str, *_args: object, **_kwargs: object) -> object:
    top_level = name.split(".")[0]
    if top_level in _ALLOWED_MODULES:
        return _ALLOWED_MODULES[top_level]
    error_code = _MODULE_DENY_CLASS.get(top_level, "filesystem_denied")
    raise _SandboxDenied(error_code, f"import of {name!r} is not allowed in sandboxed metrics")


def _restricted_globals(context: dict[str, object]) -> dict[str, object]:
    safe_builtins: dict[str, object] = {
        name: getattr(builtins, name) for name in _ALLOWED_BUILTIN_NAMES if hasattr(builtins, name)
    }
    safe_builtins["open"] = _denied_open
    safe_builtins["__import__"] = _restricted_import
    return {"__builtins__": safe_builtins, **_ALLOWED_MODULES, "context": context}


def _validate_result(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise _SandboxDenied("invalid_output", "`result` must be a dict")
    status = raw.get("status")
    if status not in ("passed", "failed", "warn"):
        raise _SandboxDenied(
            "invalid_output", f"`result['status']` must be passed/failed/warn, got {status!r}"
        )
    rationale = raw.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise _SandboxDenied("invalid_output", "`result['rationale']` must be a string or absent")
    try:
        json.dumps(raw.get("value"))
    except TypeError as e:
        raise _SandboxDenied(
            "invalid_output", f"`result['value']` is not JSON-serializable: {e}"
        ) from e
    return {"status": status, "value": raw.get("value"), "rationale": rationale}


def _execute(code: str, context: dict[str, object]) -> dict[str, object]:
    _reject_dunder_access(code)
    namespace = _restricted_globals(context)
    # User code's own stdout/stderr writes are captured and discarded, not
    # forwarded -- the real stdout is reserved for this worker's own final
    # JSON verdict, written after exec() returns, so nothing the sandboxed
    # code prints can be mistaken for (or forge) that verdict.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        exec(compile(code, "<metric>", "exec"), namespace)  # noqa: S102 -- the whole point
    if "result" not in namespace:
        raise _SandboxDenied("invalid_output", "code did not set a `result` variable")
    return _validate_result(namespace["result"])


def main() -> None:
    # Everything from here down -- including setup, not just _execute() --
    # is wrapped in one try/except so stdout always gets a clean JSON
    # response. Before this fix, a failure in setup (signal registration,
    # the audit hook, resource limits, or malformed stdin) raised
    # uncaught: an empty stdout plus a raw traceback on stderr, which
    # sandbox.py's _parse_worker_output falls back to surfacing as
    # `rationale` -- an unnecessary info leak (absolute paths, Python
    # version) for what should just be a generic, sanitized error.
    try:
        signal.signal(signal.SIGXCPU, lambda _signum, _frame: (_ for _ in ()).throw(_CPUExceeded()))
        _install_audit_hook()
        _apply_resource_limits()

        request: dict[str, Any] = json.loads(sys.stdin.buffer.read())
        code = request["code"]
        context = request.get("context", {})

        result = _execute(code, context)
        response: dict[str, object] = {"ok": True, **result}
    except _SandboxDenied as e:
        response = {"ok": False, "error_code": e.error_code, "rationale": str(e)}
    except _CPUExceeded:
        response = {"ok": False, "error_code": "cpu_exceeded", "rationale": "exceeded CPU limit"}
    except MemoryError:
        response = {
            "ok": False,
            "error_code": "memory_exceeded",
            "rationale": "exceeded memory limit",
        }
    except Exception as e:  # noqa: BLE001 -- untrusted code, anything can be raised
        response = {"ok": False, "error_code": "error", "rationale": f"{type(e).__name__}: {e}"}

    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
