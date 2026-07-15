"""Provider-neutral, controlled web-search clients.

Only the explicitly configured provider is used. In particular, a failed or
incomplete Brave configuration never falls back to Hermes because that would
send the user's query to a different processor without an explicit choice.
"""

import json
import logging
import re
import unicodedata
from http.client import HTTPException as HTTPClientException
from typing import Protocol
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

from app.assistant.hermes_web import (
    HermesWebUnavailableError,
    _clean_absolute_http_url,
    _clean_optional_string,
    hermes_web_client,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_API_VERSION = "2023-01-01"
MAX_BRAVE_RESPONSE_BYTES = 1024 * 1024
MAX_BRAVE_RESULTS = 20
MAX_WEB_QUERY_CHARS = 400
MAX_WEB_QUERY_WORDS = 50
# This is a narrow last-line guard for obvious structured identifiers, not a
# complete DLP policy. Names and postal addresses still require a separately
# reviewed policy before arbitrary free text is suitable for web search.
PERSONAL_DATA_PATTERN = re.compile(
    r"""
    (
        (?<!\w)\d(?:[\s.-]*\d){7}[\s.-]*[A-Za-z](?!\w)
        |
        (?<!\w)[XYZ][\s.-]*\d(?:[\s.-]*\d){6}[\s.-]*[A-Za-z](?!\w)
        |
        [\w.+-]+@[\w-]+\.[\w.-]+
        |
        (?<!\d)(?:(?:\+|00)34[\s.-]*)?[6789](?:[\s.-]*\d){8}(?!\d)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class WebSearchUnavailableError(Exception):
    """The selected controlled web-search provider cannot serve the request."""


class _WebSearchProvider(Protocol):
    @property
    def enabled(self) -> bool: ...

    def search(self, *, query: str, limit: int) -> list[dict[str, str | None]]: ...


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    """Reject redirects so the server-side Brave token never changes origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_brave_opener = urlrequest.build_opener(_RejectRedirects())


def normalize_web_query(value: object) -> str:
    """Normalize a public query and enforce Brave's documented input limits."""
    if not isinstance(value, str):
        raise ValueError("query debe ser texto")
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        raise ValueError("query no puede contener caracteres de control")
    query = " ".join(normalized.split())
    if not query:
        raise ValueError("query no puede estar vacío")
    if len(query) > MAX_WEB_QUERY_CHARS:
        raise ValueError(f"query no puede superar {MAX_WEB_QUERY_CHARS} caracteres")
    if len(query.split()) > MAX_WEB_QUERY_WORDS:
        raise ValueError(f"query no puede superar {MAX_WEB_QUERY_WORDS} palabras")
    if PERSONAL_DATA_PATTERN.search(query):
        raise ValueError("query no puede contener datos personales identificables")
    return query


class BraveSearchClient:
    @property
    def enabled(self) -> bool:
        """Return configuration and storage-contract readiness."""
        api_key = settings.brave_search_api_key
        if not isinstance(api_key, str) or not api_key.strip():
            return False
        return settings.brave_search_storage_rights_confirmed

    def search(self, *, query: str, limit: int) -> list[dict[str, str | None]]:
        query = normalize_web_query(query)
        if not self.enabled:
            raise WebSearchUnavailableError("Brave Search no está configurado")

        count = min(max(int(limit), 1), MAX_BRAVE_RESULTS)
        parameters = {
            "q": query,
            "count": str(count),
            "country": settings.brave_search_country,
            "search_lang": settings.brave_search_language,
            "ui_lang": settings.brave_search_ui_language,
            "safesearch": "strict",
            "text_decorations": "false",
            "result_filter": "web",
        }
        request = urlrequest.Request(
            f"{BRAVE_WEB_SEARCH_URL}?{urlencode(parameters)}",
            headers={
                "Accept": "application/json",
                "Api-Version": BRAVE_API_VERSION,
                "User-Agent": f"AsistenteAyuntamientos/{settings.app_version}",
                "X-Subscription-Token": settings.brave_search_api_key.strip(),
            },
            method="GET",
        )

        try:
            with _brave_opener.open(
                request,
                timeout=settings.brave_search_timeout_seconds,
            ) as response:
                body = response.read(MAX_BRAVE_RESPONSE_BYTES + 1)
            if len(body) > MAX_BRAVE_RESPONSE_BYTES:
                raise ValueError("response exceeds maximum size")
            response_data = json.loads(body.decode("utf-8"))
            results = _parse_brave_results(response_data, limit=count)
        except urlerror.HTTPError as error:
            logger.warning("Brave Search API error: status=%s", error.code)
            if error.code in {401, 403}:
                detail = "Brave Search rechazó las credenciales configuradas"
            elif error.code == 429:
                detail = "Brave Search ha alcanzado su límite de solicitudes"
            else:
                detail = "Brave Search no está disponible"
            raise WebSearchUnavailableError(detail) from error
        except (
            urlerror.URLError,
            TimeoutError,
            OSError,
            HTTPClientException,
        ) as error:
            logger.warning("Brave Search API connection error")
            raise WebSearchUnavailableError(
                "No se pudo conectar con Brave Search"
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            logger.warning("Brave Search API returned an invalid response")
            raise WebSearchUnavailableError(
                "Brave Search devolvió una respuesta no válida"
            ) from error

        logger.info("Brave Search completed: result_count=%s", len(results))
        return results


def _parse_brave_results(
    response_data: object,
    *,
    limit: int,
) -> list[dict[str, str | None]]:
    if not isinstance(response_data, dict):
        raise TypeError("response must be an object")
    web = response_data.get("web")
    if web is None:
        return []
    if not isinstance(web, dict):
        raise TypeError("web must be an object")
    raw_results = web.get("results", [])
    if not isinstance(raw_results, list):
        raise TypeError("web.results must be a list")

    results: list[dict[str, str | None]] = []
    seen_urls: set[str] = set()
    for raw in raw_results:
        if len(results) >= limit:
            break
        if not isinstance(raw, dict):
            continue
        url = _clean_absolute_http_url(raw.get("url"), max_length=2000)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = _clean_optional_string(raw.get("title"), max_length=300)
        snippet = _clean_optional_string(raw.get("description"), max_length=1000)
        published_at = _clean_optional_string(
            raw.get("page_age") or raw.get("age"),
            max_length=100,
        )
        results.append(
            {
                "title": title or url,
                "url": url,
                "snippet": snippet,
                "published_at": published_at,
            }
        )
    return results


class ConfiguredWebSearchClient:
    @property
    def provider_name(self) -> str:
        return settings.web_search_provider

    def _selected_client(self) -> _WebSearchProvider | None:
        if self.provider_name == "brave":
            return brave_search_client
        if self.provider_name == "hermes":
            return hermes_web_client
        return None

    @property
    def enabled(self) -> bool:
        client = self._selected_client()
        return client is not None and client.enabled

    def search(self, *, query: str, limit: int) -> list[dict[str, str | None]]:
        query = normalize_web_query(query)
        client = self._selected_client()
        if client is None or not client.enabled:
            raise WebSearchUnavailableError(
                "El proveedor de búsqueda web no está configurado"
            )
        try:
            return client.search(query=query, limit=limit)
        except WebSearchUnavailableError:
            raise
        except HermesWebUnavailableError as error:
            raise WebSearchUnavailableError(str(error)) from error


brave_search_client = BraveSearchClient()
web_search_client = ConfiguredWebSearchClient()
