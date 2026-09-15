from collections.abc import Callable
from typing import TypeVar


ACCEPTANCE_MARKER = "__local_web_acceptance__"

_Decorated = TypeVar("_Decorated", bound=Callable[..., object] | type)


def acceptance(item: _Decorated) -> _Decorated:
    """Mark a test method or class for explicit acceptance-suite selection."""
    setattr(item, ACCEPTANCE_MARKER, True)
    return item


STRESS_MARKER = "__local_web_stress__"


def stress(item: _Decorated) -> _Decorated:
    """Mark separately invoked supported-scale coverage."""
    setattr(item, STRESS_MARKER, True)
    return item
