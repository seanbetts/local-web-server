from collections.abc import Callable
from typing import TypeVar


ACCEPTANCE_MARKER = "__local_web_acceptance__"

_Decorated = TypeVar("_Decorated", bound=Callable[..., object] | type)


def acceptance(item: _Decorated) -> _Decorated:
    """Mark a test method or class for explicit acceptance-suite selection."""
    setattr(item, ACCEPTANCE_MARKER, True)
    return item
