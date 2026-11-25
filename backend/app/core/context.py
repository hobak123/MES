from __future__ import annotations

from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)
_user_agent: ContextVar[str | None] = ContextVar("user_agent", default=None)


def set_request_context(request_id: str, ip: str | None, ua: str | None) -> None:
    _request_id.set(request_id)
    _client_ip.set(ip)
    _user_agent.set(ua)


def get_request_id() -> str | None:
    return _request_id.get()


def get_client_ip() -> str | None:
    return _client_ip.get()


def get_user_agent() -> str | None:
    return _user_agent.get()
