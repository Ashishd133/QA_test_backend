"""B2.7-07 tests: the Python metric sandbox's exact named done-when -- a
socket attempt, a file read attempt, and an infinite loop each fail closed
with a distinct error code; a well-behaved metric returns quickly. No DB
needed (pure subprocess tests), so these run in CI too.
"""

import sys
import time

import pytest

from app.engine.metrics.sandbox import run_python_metric


async def test_well_behaved_metric_returns_correctly_and_quickly() -> None:
    code = """
result = {"status": "passed", "value": 1.0, "rationale": "always passes"}
"""
    start = time.monotonic()
    outcome = await run_python_metric(code, {})
    elapsed_ms = (time.monotonic() - start) * 1000

    assert outcome.ok is True
    assert outcome.status == "passed"
    assert outcome.value == 1.0
    assert elapsed_ms < 2000  # generous vs. the ticket's <500ms bar -- subprocess startup varies


async def test_metric_can_read_the_context_it_was_given() -> None:
    code = """
turns = context.get("turns", [])
result = {"status": "passed" if len(turns) == 2 else "failed", "value": len(turns)}
"""
    outcome = await run_python_metric(code, {"turns": ["hi", "hello"]})
    assert outcome.ok is True
    assert outcome.status == "passed"
    assert outcome.value == 2


async def test_socket_attempt_fails_closed_with_network_denied() -> None:
    code = """
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "network_denied"


async def test_file_read_attempt_fails_closed_with_filesystem_denied() -> None:
    code = """
f = open("/etc/passwd")
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "filesystem_denied"


async def test_subprocess_import_fails_closed_with_subprocess_denied() -> None:
    code = """
import subprocess
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "subprocess_denied"


async def test_infinite_loop_fails_closed() -> None:
    code = """
x = 0
while True:
    x = x + 1
"""
    outcome = await run_python_metric(code, {}, timeout_s=3.0)
    assert outcome.ok is False
    # SIGXCPU should fire well before the parent's own wall-clock timeout,
    # but assert on the broader "did not silently pass" bar rather than
    # exact signal-delivery timing across machines.
    assert outcome.error_code in ("cpu_exceeded", "timeout")


async def test_parent_side_wall_clock_timeout_kills_the_process_group() -> None:
    # A well-behaved metric, but with a timeout far shorter than subprocess
    # startup can possibly complete in -- exercises the parent's own
    # asyncio.wait_for + process-group-kill path specifically, independent
    # of any child-side signal.
    code = 'result = {"status": "passed", "value": 1}'
    outcome = await run_python_metric(code, {}, timeout_s=0.001)
    assert outcome.ok is False
    assert outcome.error_code == "timeout"


async def test_missing_result_variable_is_invalid_output() -> None:
    code = "x = 1 + 1"
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_output"


async def test_non_serializable_result_value_is_invalid_output() -> None:
    code = """
result = {"status": "passed", "value": {1, 2, 3}}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_output"


async def test_bad_status_value_is_invalid_output() -> None:
    code = """
result = {"status": "definitely-not-a-real-status", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_output"


async def test_raised_exception_in_metric_code_is_error_not_a_crashed_executor() -> None:
    code = """
raise ValueError("boom")
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "error"
    assert outcome.rationale is not None and "boom" in outcome.rationale


async def test_printing_does_not_forge_the_verdict() -> None:
    code = """
print('{"ok": true, "status": "passed", "value": "forged"}')
result = {"status": "failed", "value": "real"}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is True
    assert outcome.status == "failed"
    assert outcome.value == "real"


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="RLIMIT_AS is not meaningfully enforced on macOS; this only bounds memory on Linux",
)
async def test_memory_bomb_fails_closed_with_memory_exceeded() -> None:
    code = """
x = [0] * (10**9)
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {}, timeout_s=5.0)
    assert outcome.ok is False
    assert outcome.error_code in ("memory_exceeded", "error")


async def test_globals_escape_via_stdlib_function_is_rejected() -> None:
    """Regression test for the security review finding: re/statistics are
    pure-Python modules, so their functions expose `.__globals__` -- the
    function's home module dict, which CPython auto-populates with a
    `__builtins__` key pointing at the real, unrestricted builtins. This
    was a working one-line escape before `_reject_dunder_access` existed."""
    code = """
b = re.sub.__globals__['__builtins__']
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "forbidden_syntax"


async def test_dunder_name_access_is_rejected_even_without_a_leaked_module() -> None:
    code = """
result = {"status": "passed", "value": str(__builtins__)}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "forbidden_syntax"


async def test_class_hierarchy_walk_is_rejected() -> None:
    code = """
leak = ().__class__.__bases__[0].__subclasses__()
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    assert outcome.error_code == "forbidden_syntax"


async def test_os_system_via_import_fails_closed_with_subprocess_denied() -> None:
    code = """
import os
os.system("echo hi")
result = {"status": "passed", "value": None}
"""
    outcome = await run_python_metric(code, {})
    assert outcome.ok is False
    # os is not in the allowed-module map at all, so this is denied at the
    # _restricted_import layer (filesystem_denied is os's default
    # classification there) before the audit hook's os.system coverage
    # would even get a chance to fire -- both layers agree it's denied.
    assert outcome.error_code in ("filesystem_denied", "subprocess_denied")
