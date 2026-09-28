"""B2.7-07: locked-down Python metric execution -- a customer's
`spec.code` runs as untrusted input from the moment it's read from the
`metrics` table, never trusted, never exec'd in-process. Two independent
layers:

  1. OS-level (this module + sandbox_worker.py's `_apply_resource_limits`):
     a genuinely separate subprocess, its own process group, an empty
     environment, and a *parent-side* wall-clock timeout that kills the
     whole process group -- the one backstop that catches an I/O-bound
     hang (RLIMIT_CPU only catches CPU-bound loops; `time.sleep(9999)`
     burns no CPU at all, so the child-side CPU limit alone cannot bound
     it).
  2. Interpreter-level, inside the child, defense in depth: a restricted
     builtins dict (no open/eval/exec/__import__) and a
     `sys.addaudithook` that intercepts socket/file/subprocess events and
     raises a classified error -- this is also the *only* thing that can
     tell a network attempt apart from a filesystem attempt, since both
     would otherwise fail identically against a low RLIMIT_NOFILE (both
     allocate a file descriptor).

Every failure mode maps to a distinct `SandboxResult.error_code`; there is
no path that reports a passing verdict for code that didn't run to
completion inside every one of these limits. Treat this as untrusted code
execution, because it is -- this module's diff needs a dedicated
security-focused review pass beyond normal code review before it's ever
reachable from an endpoint a customer can hit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DEFAULT_TIMEOUT_SECONDS = 2.0
_WORKER_MODULE = "app.engine.metrics.sandbox_worker"
# app/engine/metrics/sandbox.py -> project root, four parents up.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

ErrorCode = Literal[
    "timeout",
    "cpu_exceeded",
    "memory_exceeded",
    "network_denied",
    "filesystem_denied",
    "subprocess_denied",
    "invalid_output",
    "forbidden_syntax",
    "error",
]


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    status: Literal["passed", "failed", "warn", "error"]
    value: object | None
    rationale: str | None
    error_code: ErrorCode | None


async def run_python_metric(
    code: str,
    context: dict[str, object],
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_SECONDS,
) -> SandboxResult:
    request = json.dumps({"code": code, "context": context}).encode()

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        # No -I here (isolated mode implies -E, which ignores PYTHONPATH
        # entirely) -- this process still needs `-m app.engine.metrics.
        # sandbox_worker` to resolve, via `cwd` below, and -I would break
        # that. -S (no `site` import) is independent of that resolution
        # path and stays: it skips importing user/global site-packages,
        # minimizing what's importable before any restriction is applied.
        "-S",
        "-m",
        _WORKER_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Empty environment: the parent process's own env holds
        # DATABASE_URL, FIELD_ENCRYPTION_KEYS, Google credentials --
        # passing any of that to untrusted code would be the single
        # highest-impact mistake this module could make.
        env={},
        # `-m` needs the project root on sys.path to resolve `app.engine.
        # metrics.sandbox_worker` -- with an empty env (no PYTHONPATH) the
        # only way to give it that is via cwd, computed from this file's
        # own location rather than trusted to the parent process's ambient
        # working directory.
        cwd=_PROJECT_ROOT,
        # Own process group, so a timeout kills every descendant (a forked/
        # exec'd grandchild, however unlikely given RLIMIT_NPROC + the
        # audit hook) rather than leaving one orphaned and running.
        start_new_session=True,
    )
    assert proc.pid is not None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=request), timeout=timeout_s)
    except TimeoutError:
        _kill_process_group(proc.pid)
        await proc.wait()
        return SandboxResult(
            ok=False,
            status="error",
            value=None,
            rationale=f"exceeded {timeout_s}s wall-clock limit",
            error_code="timeout",
        )

    return _parse_worker_output(stdout, stderr, proc.returncode)


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, 9)  # SIGKILL -- nothing left to trust for a graceful shutdown here
    except OSError:
        # ProcessLookupError (already exited) is the expected case; a
        # broader OSError (e.g. PermissionError on some platform-specific
        # edge case) must not crash the parent either -- there is nothing
        # further this function can do about it, and the timeout verdict
        # is still returned to the caller either way.
        pass


def _parse_worker_output(stdout: bytes, stderr: bytes, returncode: int | None) -> SandboxResult:
    if not stdout.strip():
        rationale = stderr.decode(errors="replace")[-2000:] or f"no output (exit {returncode})"
        return SandboxResult(
            ok=False, status="error", value=None, rationale=rationale, error_code="error"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return SandboxResult(
            ok=False,
            status="error",
            value=None,
            rationale="worker produced non-JSON output",
            error_code="invalid_output",
        )
    if not isinstance(payload, dict) or "ok" not in payload:
        return SandboxResult(
            ok=False,
            status="error",
            value=None,
            rationale="worker output missing required fields",
            error_code="invalid_output",
        )
    if payload["ok"]:
        return SandboxResult(
            ok=True,
            status=payload["status"],
            value=payload.get("value"),
            rationale=payload.get("rationale"),
            error_code=None,
        )
    return SandboxResult(
        ok=False,
        status="error",
        value=None,
        rationale=payload.get("rationale"),
        error_code=payload.get("error_code", "error"),
    )
