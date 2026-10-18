"""Approved Python snapshots in bounded, cooperative worker threads (WD-143).

This is trusted code in the daemon process, not a sandbox. A supervisor timeout
does not stop a thread; its slot remains occupied until it actually exits.
"""

import ast
import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import Field, ValidationError, model_validator

from agent_watchdog.rules.api import ALLOW, Context, Decision
from agent_watchdog.rules.declarative import DeclarativeRule, RuleError


class PythonRule(DeclarativeRule):
    """Literal subscription/action metadata; discovery never imports the module."""

    timeout_ms: Annotated[int, Field(gt=0, le=60_000)] = 100

    @model_validator(mode="after")
    def python_predicates(self) -> Self:
        if (
            self.state.used()
            or self.match.input_regex is not None
            or self.match.error_regex is not None
        ):
            raise ValueError("Python conditions belong in evaluate; metadata only selects tools")
        return self


def parse_python(snapshot: bytes) -> PythonRule:
    """Compile for syntax and read a literal ``RULE`` dictionary without execution."""
    try:
        text = snapshot.decode("utf-8")
        tree = ast.parse(text, filename="<watchdog-rule>")
        compile(tree, "<watchdog-rule>", "exec")
        definitions = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "RULE" for target in node.targets)
        ]
        if len(definitions) != 1:
            raise ValueError("One literal RULE dictionary is required")
        return PythonRule.model_validate(ast.literal_eval(definitions[0]))
    except (
        UnicodeDecodeError,
        SyntaxError,
        ValueError,
        TypeError,
        RecursionError,
        ValidationError,
    ):
        raise RuleError(
            "Invalid Python rule: expected valid syntax and one literal RULE dictionary"
        ) from None


ErrorHandler = Callable[[str, Exception], None]


class PythonRuleTimeout(TimeoutError):
    """The supervisor disabled a rule after exhausting its waiting budget."""


def _report(on_error: ErrorHandler | None, name: str, error: Exception) -> None:
    if on_error is not None:
        try:
            on_error(name, error)
        except Exception:  # noqa: BLE001 - a diagnostic must not close the channel
            pass


def _decision(result: object, rule: PythonRule) -> Decision:
    if type(result) is not Decision:
        raise ValueError("Python rule must return a Decision")
    if result.action == "allow":
        return ALLOW
    if result.action != rule.action.kind:
        raise ValueError("Python rule returned an undeclared action")
    reason, context = result.reason or rule.action.message, result.context
    for text in (reason, context):
        if text is not None and (not isinstance(text, str) or len(text) > 1000):
            raise ValueError("Invalid Python decision text")
    updated = None
    if result.action == "rewrite":
        if not isinstance(result.updated_input, Mapping):
            raise ValueError("Rewrite requires updated_input")
        encoded = json.dumps(dict(result.updated_input), allow_nan=False)
        if len(encoded.encode()) > 64 * 1024:
            raise ValueError("Rewrite is too large")
        updated = json.loads(encoded)
    # Attribution belongs to the supervisor, never to arbitrary returned fields.
    return Decision(
        action=result.action,
        rule=rule.name,
        rule_version=rule.version,
        reason=reason,
        context=(context or reason) if result.action == "context" else None,
        updated_input=updated,
    )


@dataclass(slots=True)
class _Flight:
    thread: threading.Thread
    supervised: bool = True


class PythonHost:
    """One daemon lifetime's execution capacity and per-rule health.

    There is no work queue. Busy or exhausted capacity fails open immediately.
    Disabled names survive byte changes and completed late workers until restart.
    """

    def __init__(self, max_workers: int = 4) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._running: dict[str, _Flight] = {}
        self._health: dict[str, str] = {}

    def status(self, name: str) -> str | None:
        with self._lock:
            return self._health.get(name)

    def evaluate(
        self,
        rule: PythonRule,
        snapshot: bytes,
        approved_digest: str | None,
        ctx: Context,
        *,
        on_error: ErrorHandler | None = None,
    ) -> Decision:
        if approved_digest is None or hashlib.sha256(snapshot).hexdigest() != approved_digest:
            return ALLOW
        deadline = time.monotonic() + rule.timeout_ms / 1000
        results: queue.SimpleQueue[Decision | BaseException] = queue.SimpleQueue()
        done = threading.Event()

        def execute() -> None:
            try:
                namespace: dict = {"__name__": f"watchdog_rule_{rule.name}"}
                # Compile and execute only the already verified byte snapshot.
                exec(compile(snapshot, f"<watchdog-rule:{rule.name}>", "exec"), namespace)
                result = namespace["evaluate"](ctx.readonly())
                results.put(_decision(result, rule))
            except BaseException as error:  # rule SystemExit must not escape its worker
                results.put(error)
            finally:
                done.set()

        with self._lock:
            self._running = {
                name: flight
                for name, flight in self._running.items()
                if flight.supervised or flight.thread.is_alive()
            }
            if (
                self._health.get(rule.name) == "disabled"
                or rule.name in self._running
                or len(self._running) >= self._max_workers
            ):
                return ALLOW
            worker = threading.Thread(
                target=execute, name=f"watchdog-rule:{rule.name}", daemon=True
            )
            flight = self._running[rule.name] = _Flight(worker)
            try:
                worker.start()
            except RuntimeError:
                self._running.pop(rule.name, None)
                self._health[rule.name] = "degraded"
                return ALLOW

        finished = done.wait(max(0, deadline - time.monotonic()))
        if not finished or time.monotonic() > deadline:
            with self._lock:
                self._health[rule.name] = "disabled"
                flight.supervised = False
            _report(
                on_error,
                rule.name,
                PythonRuleTimeout("Python rule timed out; disabled until daemon restart"),
            )
            return ALLOW
        result = results.get_nowait()
        if isinstance(result, BaseException):
            with self._lock:
                self._health[rule.name] = "degraded"
                flight.supervised = False
            error = (
                result
                if isinstance(result, Exception)
                else RuntimeError(f"Python rule failed: {type(result).__name__}")
            )
            _report(on_error, rule.name, error)
            return ALLOW
        with self._lock:
            self._health.pop(rule.name, None)
            flight.supervised = False
        return result
