"""Controlled wrapper for Hermes' native web-search capability.

The application does not expose Hermes toolsets directly to end users. This
client talks to a separate local Hermes API Server profile/instance whose
api_server platform should expose only the `web` toolset. Backend RBAC and
audit still decide whether a user can request a search.
"""

import json
import logging
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.http import urlopen_without_redirects

logger = logging.getLogger(__name__)


class HermesWebUnavailableError(Exception):
    """The controlled Hermes Web runtime is not configured or failed."""


class HermesWebClient:
    @property
    def enabled(self) -> bool:
        """Return local configuration readiness without making a network call."""
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                settings.hermes_web_base_url,
                settings.hermes_web_api_key,
                settings.hermes_web_model,
            )
        )

    def search(self, *, query: str, limit: int) -> list[dict[str, str | None]]:
        if not self.enabled:
            raise HermesWebUnavailableError("Hermes Web is not configured")

        payload: dict[str, Any] = {
            "model": settings.hermes_web_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres un adaptador de búsqueda web para un backend. "
                        "Usa únicamente tus herramientas web disponibles para "
                        "buscar información pública. No uses memoria, archivos, "
                        "terminal, navegador ni acciones externas. Devuelve "
                        "exclusivamente JSON válido con esta forma: "
                        '{"results":[{"title":"...","url":"...",'
                        '"snippet":"...","published_at":null}]}. '
                        "No incluyas texto fuera del JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"query": query, "limit": limit},
                        ensure_ascii=False,
                    ),
                },
            ],
            "max_tokens": 4000,
            "stream": False,
        }
        request = urlrequest.Request(
            _hermes_web_url("chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers=_hermes_web_headers(),
            method="POST",
        )

        try:
            with urlopen_without_redirects(
                request,
                timeout=settings.hermes_web_timeout_seconds,
            ) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            content = _message_content(response_data)
            results = _parse_results(content, limit=limit)
        except urlerror.HTTPError as error:
            logger.error("Hermes Web API error: status=%s", error.code)
            raise HermesWebUnavailableError(
                "Hermes Web API request failed"
            ) from error
        except (urlerror.URLError, TimeoutError) as error:
            logger.error("Hermes Web API connection error")
            raise HermesWebUnavailableError(
                "Hermes Web API connection failed"
            ) from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            logger.error("Hermes Web API returned an invalid response")
            raise HermesWebUnavailableError(
                "Hermes Web API returned an invalid response"
            ) from error

        logger.info(
            "Hermes Web search completed: result_count=%s",
            len(results),
        )
        return results


def _hermes_web_url(path: str) -> str:
    base_url = settings.hermes_web_base_url.rstrip("/")
    clean_path = path.strip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/{clean_path}"
    return f"{base_url}/v1/{clean_path}"


def _hermes_web_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.hermes_web_api_key:
        headers["Authorization"] = f"Bearer {settings.hermes_web_api_key}"
    return headers


def _message_content(response_data: dict[str, Any]) -> str:
    content = response_data["choices"][0]["message"].get("content") or ""
    if not isinstance(content, str):
        raise TypeError("message content must be a string")
    return content.strip()


def _parse_results(content: str, *, limit: int) -> list[dict[str, str | None]]:
    payload = json.loads(_extract_json_object(content))
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise TypeError("results must be a list")

    results: list[dict[str, str | None]] = []
    for raw in raw_results[:limit]:
        if not isinstance(raw, dict):
            continue
        title = _clean_optional_string(raw.get("title"), max_length=300)
        url = _clean_absolute_http_url(raw.get("url"), max_length=2000)
        snippet = _clean_optional_string(raw.get("snippet"), max_length=1000)
        published_at = _clean_optional_string(
            raw.get("published_at"),
            max_length=100,
        )
        if not url:
            continue
        results.append(
            {
                "title": title or url,
                "url": url,
                "snippet": snippet,
                "published_at": published_at,
            }
        )
    return results


def _extract_json_object(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found")
    return stripped[start : end + 1]


def _clean_optional_string(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_length]


def _clean_absolute_http_url(value: Any, *, max_length: int) -> str | None:
    """Accept only browser-safe absolute HTTP(S) source URLs."""
    if value is None:
        return None
    text = str(value).strip()
    if (
        not text
        or len(text) > max_length
        or any(char.isspace() or ord(char) < 32 for char in text)
    ):
        return None

    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        # Accessing port also rejects malformed values such as ``:not-a-port``.
        parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or not hostname.strip(".")
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return text


hermes_web_client = HermesWebClient()
