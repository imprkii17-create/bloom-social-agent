"""Thin, careful HTTP client for Meta's Graph APIs (graph.instagram.com / graph.facebook.com).

- Retries only what is safe to retry (reads, and writes that Meta marks as transient *before* doing anything).
- Never puts access tokens in logs or exception messages.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Graph error codes that mean "try again later" (rate limits, temporary outages).
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 80001, 80002}
# Throttling: Meta refused before doing anything, so even a non-idempotent POST may be repeated.
RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80002}


class GraphError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        transient: bool = False,
        fbtrace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode
        self.transient = transient
        self.fbtrace_id = fbtrace_id

    @property
    def is_auth_error(self) -> bool:
        return self.code == 190 or self.status == 401

    def __str__(self) -> str:
        bits = [super().__str__()]
        if self.code is not None:
            bits.append(f"(code {self.code}{f'/{self.subcode}' if self.subcode else ''})")
        if self.fbtrace_id:
            bits.append(f"[trace {self.fbtrace_id}]")
        return " ".join(bits)


class GraphClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = access_token
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout, transport=transport, headers={"User-Agent": "bloom-agent/1.0"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------------

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params, retry_network=True)

    def post(self, path: str, data: Mapping[str, Any] | None = None, *, retry_network: bool = False) -> dict[str, Any]:
        """POSTs are only retried on network errors when the caller says it is safe (idempotent)."""
        return self._request("POST", path, data=data, retry_network=retry_network)

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    # ------------------------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        retry_network: bool,
    ) -> dict[str, Any]:
        url = self._url(path)
        # Meta documents the token as an `access_token` parameter on both hosts, so we send it that way.
        query = {k: _stringify(v) for k, v in (params or {}).items() if v is not None}
        body = {k: _stringify(v) for k, v in (data or {}).items() if v is not None}
        if method == "GET":
            query["access_token"] = self._token
        else:
            body["access_token"] = self._token
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, url, params=query, data=body or None)
            except httpx.TransportError as exc:
                if retry_network and attempt <= self._max_retries:
                    self._backoff(attempt, f"network error: {type(exc).__name__}")
                    continue
                raise GraphError(f"Network error talking to Meta: {type(exc).__name__}", transient=True) from None

            if resp.status_code < 400:
                try:
                    payload = resp.json()
                except ValueError:
                    raise GraphError(f"Unexpected non-JSON response ({resp.status_code})", status=resp.status_code) from None
                return payload if isinstance(payload, dict) else {"data": payload}

            err = self._parse_error(resp)
            # Reads and idempotent writes may be retried on any transient error. Other writes only when throttled:
            # a 5xx could mean "done, but the answer was lost", and repeating it could post twice.
            retry_ok = method == "GET" or retry_network or err.code in RATE_LIMIT_CODES
            if err.transient and retry_ok and attempt <= self._max_retries:
                self._backoff(attempt, str(err))
                continue
            raise err

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(2 ** attempt, 30)
        log.warning("Retrying in %ss (attempt %s): %s", delay, attempt, self.redact(reason))
        self._sleep(delay)

    def _parse_error(self, resp: httpx.Response) -> GraphError:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = error.get("code")
        transient = bool(error.get("is_transient")) or code in TRANSIENT_CODES or resp.status_code >= 500
        message = error.get("error_user_msg") or error.get("message") or f"HTTP {resp.status_code}"
        return GraphError(
            self.redact(str(message)),
            status=resp.status_code,
            code=code,
            subcode=error.get("error_subcode"),
            transient=transient,
            fbtrace_id=error.get("fbtrace_id"),
        )


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)
