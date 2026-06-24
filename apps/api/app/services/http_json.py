from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HttpJsonError(RuntimeError):
    pass


def get_json(url: str, params: dict[str, object] | None = None, headers: dict[str, str] | None = None, timeout: int = 15):
    query = urlencode(params or {}, doseq=True)
    full_url = f"{url}?{query}" if query else url
    request = Request(full_url, headers=headers or {}, method="GET")

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HttpJsonError(f"GET {full_url} failed with HTTP {exc.code}: {detail[:300]}") from exc
    except URLError as exc:
        raise HttpJsonError(f"GET {full_url} failed: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpJsonError(f"GET {full_url} returned invalid JSON.") from exc
