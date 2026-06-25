from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HttpJsonError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        params: dict[str, object],
        status_code: int | None = None,
        body_preview: str | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.params = params
        self.status_code = status_code
        self.body_preview = body_preview

    def to_detail(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "query_params": self.params,
            "upstream_status_code": self.status_code,
            "body_preview": self.body_preview,
        }


def get_json(url: str, params: dict[str, object] | None = None, headers: dict[str, str] | None = None, timeout: int = 15):
    query_params = params or {}
    query = urlencode(query_params, doseq=True)
    full_url = f"{url}?{query}" if query else url
    request = Request(full_url, headers=headers or {}, method="GET")

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HttpJsonError(
            f"GET {url} failed with HTTP {exc.code}.",
            endpoint=url,
            params=dict(query_params),
            status_code=exc.code,
            body_preview=detail[:300],
        ) from exc
    except URLError as exc:
        raise HttpJsonError(
            f"GET {url} failed: {exc.reason}",
            endpoint=url,
            params=dict(query_params),
            body_preview=str(exc.reason)[:300],
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpJsonError(
            f"GET {url} returned invalid JSON.",
            endpoint=url,
            params=dict(query_params),
            body_preview=body[:300],
        ) from exc
