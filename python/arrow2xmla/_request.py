"""XMLA requests: what a client sent, as frozen dataclasses (match on them)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from arrow2xmla import _native


class SessionKind(str, Enum):
    """What the SOAP header says about the session."""

    NONE = "none"  # no session header
    BEGIN = "begin"  # the server makes an id
    USE = "use"  # the client's own id, to echo
    END = "end"  # the client ends the session


@dataclass(frozen=True, slots=True)
class Session:
    """The session header: `id` is set for USE, and for END when the client names one."""

    kind: SessionKind
    id: str | None = None


@dataclass(frozen=True, slots=True)
class Discover:
    """A request for a rowset of metadata."""

    request_type: str
    restrictions: tuple[tuple[str, tuple[str, ...]], ...]  # name -> values, in order
    properties: tuple[tuple[str, str], ...]  # the PropertyList, in order

    def restriction(self, name: str) -> tuple[str, ...]:
        """The values of a restriction (the last one when it is repeated); empty when absent."""
        return next((v for k, v in reversed(self.restrictions) if k == name), ())

    def property(self, name: str) -> str | None:
        """A property's value (the last one when it is repeated)."""
        return next((v for k, v in reversed(self.properties) if k == name), None)


@dataclass(frozen=True, slots=True)
class Execute:
    """An MDX statement."""

    statement: str
    properties: tuple[tuple[str, str], ...]

    def property(self, name: str) -> str | None:
        """A property's value (the last one when it is repeated)."""
        return next((v for k, v in reversed(self.properties) if k == name), None)


@dataclass(frozen=True, slots=True)
class Other:
    """A body with neither a Discover nor an Execute."""


@dataclass(frozen=True, slots=True)
class Request:
    """A parsed request: the session header and the first Discover or Execute."""

    session: Session
    call: Discover | Execute | Other


# the longest request read: a real one is a few KB
DEFAULT_MAX_SIZE: int = _native.DEFAULT_MAX_REQUEST


def parse_request(body: bytes, max_size: int = DEFAULT_MAX_SIZE) -> Request:
    """Read an XMLA request (a SOAP envelope holding a Discover or an Execute), UTF-8 or UTF-16 with a BOM.

    Raises `DoctypeError` for a DOCTYPE (never accepted: no entity expansion), `RequestTooLarge` for a body longer
    than `max_size` bytes (refused before it is read) and `RequestError` for anything else that is not accepted.
    """
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 0:
        raise ValueError(f"max_size is a number of bytes, 0 or more, not {max_size!r}")
    r = _native.parse_request(body, min(max_size, 1 << 62))
    kind, sid = r["session"]
    c = r["call"]
    match c["kind"]:
        case "discover":
            call: Discover | Execute | Other = Discover(
                c["request_type"], tuple((k, tuple(v)) for k, v in c["restrictions"]), tuple(map(tuple, c["properties"]))
            )
        case "execute":
            call = Execute(c["statement"], tuple(map(tuple, c["properties"])))
        case _:
            call = Other()
    return Request(Session(SessionKind(kind), sid), call)
