"""Sonda de salud del contenedor del backend.

Existe como script y no como `curl` en el CMD porque la imagen base
(`python:3.12-slim`) no trae curl y añadirlo solo para esto amplía la superficie.

Manda la cabecera `Host` correcta: en producción `TrustedHostMiddleware` acepta
solo los hostnames configurados, así que una sonda contra 127.0.0.1 sin `Host`
recibiría un 400 y marcaría el contenedor como enfermo estando sano. Ver ADR-035.
"""

import os
import sys
from urllib import error, request

PORT = os.environ.get("HEALTHCHECK_PORT", "8000")
PATH = os.environ.get("HEALTHCHECK_PATH", "/health")


def expected_host() -> str | None:
    """Primer hostname de ALLOWED_HOSTS, o None si acepta cualquiera."""
    configured = os.environ.get("ALLOWED_HOSTS", "*")
    hosts = [host.strip() for host in configured.split(",") if host.strip()]
    if not hosts or "*" in hosts:
        return None
    return hosts[0]


def main() -> int:
    url = f"http://127.0.0.1:{PORT}{PATH}"
    headers = {}
    host = expected_host()
    if host is not None:
        headers["Host"] = host

    try:
        with request.urlopen(
            request.Request(url, headers=headers),
            timeout=5,
        ) as response:
            return 0 if 200 <= response.status < 300 else 1
    except (error.HTTPError, error.URLError, TimeoutError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
