"""Middleware de seguridad para el backend expuesto en un dominio público.

Están escritos como middleware ASGI puro y no sobre `BaseHTTPMiddleware`
porque el asistente responde por SSE: `BaseHTTPMiddleware` se interpone en el
streaming y arruinaría el turno conversacional en directo. Ver ADR-036.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Rutas de la documentación interactiva: solo existen en desarrollo y Swagger UI
# no puede cargar con la CSP restrictiva de la API.
DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})

# Las respuestas de la API nunca son documentos navegables: si un fichero subido
# se sirviera con un content-type ejecutable, esta CSP lo neutraliza.
API_CONTENT_SECURITY_POLICY = "default-src 'none'; sandbox; frame-ancestors 'none'"


class SecurityHeadersMiddleware:
    """Sella cada respuesta con las cabeceras de seguridad mínimas."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_docs = scope.get("path", "") in DOCS_PATHS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                if not is_docs:
                    headers.setdefault(
                        "Content-Security-Policy",
                        API_CONTENT_SECURITY_POLICY,
                    )
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class OriginCsrfMiddleware:
    """Rechaza escrituras cuyo `Origin` no sea uno de los nuestros.

    La sesión viaja en una cookie `SameSite=Lax`, que ya frena el POST de
    formulario entre sitios, pero era la única defensa CSRF del proyecto. Este
    middleware añade la comprobación de origen como segunda capa.

    Si no llega `Origin` se permite: los clientes de API con `Authorization:
    Bearer`, el webhook de Telegram y el `TestClient` no lo envían, y un
    navegador sí lo envía en toda escritura. Por eso la ausencia no es señal de
    ataque y su presencia con valor ajeno sí lo es.
    """

    def __init__(self, app: ASGIApp, *, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            await self._reject(scope, receive, send)
            return
        if origin is None and headers.get("sec-fetch-site") == "cross-site":
            await self._reject(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "Cross-site request rejected"},
            status_code=403,
        )
        await response(scope, receive, send)
