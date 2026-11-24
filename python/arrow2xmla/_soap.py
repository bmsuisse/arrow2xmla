"""The SOAP envelope around an answer, and the fault that answers a request that failed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from arrow2xmla import _native


class Method(str, Enum):
    """What an answer is to: the response element is `DiscoverResponse` or `ExecuteResponse`."""

    DISCOVER = "Discover"
    EXECUTE = "Execute"


@dataclass(frozen=True, slots=True)
class Envelope:
    """The envelope of an answer. A small answer is `wrap`ped; a big one is sent between `head()` and `tail()` while it
    is written (`Cellset.iter_xml()` in between). `session`: the id to answer with (the client's own, or a new one for
    `BeginSession`); none writes no session header."""

    method: Method
    session: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", Method(self.method))
        if self.session is not None and not isinstance(self.session, str):
            raise TypeError("Envelope.session is a str or None")

    def head(self) -> bytes:
        """Everything before the answer."""
        return _native.envelope(self.method.value, self.session, "head")

    def tail(self) -> bytes:
        """Everything after the answer."""
        return _native.envelope(self.method.value, self.session, "tail")

    def wrap(self, body: bytes) -> bytes:
        """`head()`, `body` and `tail()` as one."""
        return _native.envelope(self.method.value, self.session, "wrap", memoryview(body).tobytes())


DEFAULT_FAULT_CODE = 0xC1100000


def fault(message: str, *, code: int = DEFAULT_FAULT_CODE, source: str = "arrow2xmla", session: str | None = None) -> bytes:
    """A SOAP fault (send it with HTTP status 500), in the form Analysis Services gives: `faultcode`
    `XMLAnalysisError.0x<code>`, the message, and the error in `detail`."""
    if not isinstance(message, str):
        raise TypeError("the message of a fault is a str")
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 0xFFFFFFFF:
        raise ValueError("the code of a fault is an unsigned 32-bit number")
    return _native.fault(message, code, source, session)
