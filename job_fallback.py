# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Isolated candidate: bounded metadata recovery scoped to one user operation.

No production imports, network, files, account rotation, or persistent preferences.
Adapters classify their constructed errors; this coordinator never interprets
server bodies or guesses whether a session is usable from message substrings.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from inspect import iscoroutinefunction
from threading import RLock
from time import sleep
from typing import Callable, Iterator, TypeVar


class Route(Enum):
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"
    THIRD = "third"


class FailureKind(Enum):
    PROTOCOL = "protocol"
    TRANSIENT = "transient"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    PERMISSION = "permission"
    UNKNOWN = "unknown"


class ClassifiedFailure(RuntimeError):
    kind = FailureKind.UNKNOWN


class ProtocolFailure(ClassifiedFailure):
    kind = FailureKind.PROTOCOL


class TransientFailure(ClassifiedFailure):
    kind = FailureKind.TRANSIENT


class AuthenticationFailure(ClassifiedFailure):
    kind = FailureKind.AUTH


class RateLimitFailure(ClassifiedFailure):
    kind = FailureKind.RATE_LIMIT


class PermissionFailure(ClassifiedFailure):
    kind = FailureKind.PERMISSION


def classify_failure(error: Exception) -> FailureKind:
    """Only explicit error types are eligible; unknown failures fail closed."""
    return error.kind if isinstance(error, ClassifiedFailure) else FailureKind.UNKNOWN


@dataclass(frozen=True)
class RecoveryPolicy:
    transient_retries: int = 1
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (isinstance(self.transient_retries, bool)
                or not isinstance(self.transient_retries, int)
                or not 0 <= self.transient_retries <= 3):
            raise ValueError("transient_retries must be an integer between zero and three")
        for value in (self.initial_delay_seconds, self.max_delay_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("retry delays must be finite nonnegative numbers")
            if not 0 <= value <= 60:
                raise ValueError("retry delays must be finite and at most sixty seconds")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("initial delay cannot exceed maximum delay")


@dataclass
class OperationState:
    """One mutable reference survives ContextVar copies into tasks and threads."""
    _route: Route = field(default=Route.PRIMARY, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def route(self) -> Route:
        with self._lock:
            return self._route


_CURRENT: ContextVar[OperationState | None] = ContextVar(
    "isolated_metadata_operation", default=None
)


def current_operation() -> OperationState | None:
    return _CURRENT.get()


@contextmanager
def operation_scope() -> Iterator[OperationState]:
    """Reuse an enclosing group; create and discard a primary state otherwise."""
    existing = current_operation()
    if existing is not None:
        yield existing
        return
    state = OperationState()
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)


_T = TypeVar("_T")


def scoped_operation(function):
    """Keep a scope alive for a complete sync call or awaited async operation.

    Decorate the outer group entry and individual review entry. Nested reviews
    inherit the group's state. A sync ContextDecorator alone must not decorate
    async functions: it exits before the returned coroutine is awaited.
    """
    if iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with operation_scope():
                return await function(*args, **kwargs)
        return asynchronous

    @wraps(function)
    def synchronous(*args, **kwargs):
        with operation_scope():
            return function(*args, **kwargs)
    return synchronous


def _kind(error: Exception, classify: Callable[[Exception], FailureKind]) -> FailureKind:
    result = classify(error)
    if not isinstance(result, FailureKind):
        raise TypeError("classifier must return a FailureKind")
    return result


def _call_bounded(
    callback: Callable[[], _T], *, classify: Callable[[Exception], FailureKind],
    policy: RecoveryPolicy, sleeper: Callable[[float], None]
) -> _T:
    for attempt in range(policy.transient_retries + 1):
        try:
            return callback()
        except Exception as error:
            if (_kind(error, classify) is not FailureKind.TRANSIENT
                    or attempt == policy.transient_retries):
                raise
            sleeper(min(policy.initial_delay_seconds * (2 ** attempt),
                        policy.max_delay_seconds))
    raise AssertionError("bounded loop unexpectedly exhausted")


def run_metadata(
    primary: Callable[[], _T], alternative: Callable[[], _T] | None, *,
    third: Callable[[], _T] | None = None,
    classify: Callable[[Exception], FailureKind] = classify_failure,
    policy: RecoveryPolicy = RecoveryPolicy(),
    sleeper: Callable[[float], None] = sleep,
) -> _T:
    """Return validated metadata through bounded, operation-scoped recovery.

    A protocol mismatch switches immediately. Transient failures get bounded
    retries before switching. Authentication, rate, permission, disk, code, and
    cancellation failures do not select another route. Selection only moves
    forward: primary, alternative, then the optional third reader. A later call
    in the same operation starts at the selected route; a new operation starts
    at primary. Missing routes are never selected, and exhausting a selected
    last route does not silently return to an earlier reader.
    No result-level validation happens here: each adapter must validate identity,
    item IDs, and its recognized schema before returning (including valid empty).
    """
    if current_operation() is None:
        with operation_scope():
            return run_metadata(primary, alternative, third=third, classify=classify,
                                policy=policy, sleeper=sleeper)
    state = current_operation()
    assert state is not None
    with state._lock:
        readers = (
            (Route.PRIMARY, primary),
            (Route.ALTERNATIVE, alternative),
            (Route.THIRD, third),
        )
        index = next(i for i, (route, _) in enumerate(readers) if route is state._route)
        while True:
            route, callback = readers[index]
            if callback is None:
                following = next((i for i in range(index + 1, len(readers))
                                  if readers[i][1] is not None), None)
                if following is None:
                    raise ProtocolFailure("Selected reader is unavailable for this operation")
                index = following
                state._route = readers[index][0]
                continue
            try:
                return _call_bounded(callback, classify=classify, policy=policy, sleeper=sleeper)
            except Exception as error:
                if _kind(error, classify) not in {FailureKind.PROTOCOL, FailureKind.TRANSIENT}:
                    raise
                following = next((i for i in range(index + 1, len(readers))
                                  if readers[i][1] is not None), None)
                if following is None:
                    raise
                index = following
                state._route = readers[index][0]
