"""Ayudas de urllib para llamadas salientes que llevan credenciales."""

from urllib import request as urlrequest


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    """Rechaza redirecciones para no reenviar nunca una credencial."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen_without_redirects(request, *, timeout: float):
    """Abre `request` rechazando cualquier redirección.

    urllib repite las cabeceras originales contra el nuevo destino, así que
    seguir un 3xx entregaría la clave de API a quien controle la redirección.
    Las llamadas que viajan con `Authorization`, `Ocp-Apim-Subscription-Key` o
    equivalentes deben usar esta función en lugar de `urlopen`. Ver ADR-032.
    """
    opener = urlrequest.build_opener(_RejectRedirects())
    return opener.open(request, timeout=timeout)
