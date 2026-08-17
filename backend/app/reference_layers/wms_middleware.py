from __future__ import annotations

import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REFERENCE_WMS_PATH = re.compile(
    r"^/organizations/[^/]+/reference-layers/[^/]+/"
    r"(?:tiles/[^/]+/[^/]+/[^/]+\.png|legend\.png|identify)$"
)
AUTH_VARY_HEADERS = ("Authorization", "Cookie")


class ReferenceWMSVaryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or REFERENCE_WMS_PATH.fullmatch(
            scope.get("path", "")
        ) is None:
            await self.app(scope, receive, send)
            return

        async def send_with_private_vary(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                current = [
                    item.strip()
                    for item in headers.get("Vary", "").split(",")
                    if item.strip()
                ]
                if "*" not in current:
                    seen = {item.casefold() for item in current}
                    for item in AUTH_VARY_HEADERS:
                        if item.casefold() not in seen:
                            current.append(item)
                            seen.add(item.casefold())
                    headers["Vary"] = ", ".join(current)
            await send(message)

        await self.app(scope, receive, send_with_private_vary)
