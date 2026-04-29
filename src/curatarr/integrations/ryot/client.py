"""Small GraphQL transport boundary for Ryot."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from curatarr.adapters import AdapterError


class RyotError(AdapterError):
    """Raised when Ryot transport or GraphQL execution fails."""


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RyotClient:
    """Minimal GraphQL client for Ryot.

    Tests may inject `transport` so adapter behavior can be verified without
    network access. The default transport uses the standard library only.
    """

    url: str
    api_key: str
    transport: Transport | None = None

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        if self.transport is not None:
            response = self.transport(query, payload["variables"])
        else:
            response = self._http_execute(payload)
        return _unwrap_graphql_response(response)

    def _http_execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RyotError(f"Ryot HTTP error {exc.code}") from exc
        except URLError as exc:
            raise RyotError(f"Ryot connection error: {exc.reason}") from exc
        except OSError as exc:
            raise RyotError(f"Ryot transport error: {exc}") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RyotError("Ryot returned invalid JSON.") from exc
        if not isinstance(decoded, dict):
            raise RyotError("Ryot returned a non-object GraphQL response.")
        return decoded


def _unwrap_graphql_response(response: dict[str, Any]) -> dict[str, Any]:
    errors = response.get("errors")
    if errors:
        raise RyotError(f"Ryot GraphQL errors: {errors}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise RyotError("Ryot GraphQL response did not contain a data object.")
    return data
