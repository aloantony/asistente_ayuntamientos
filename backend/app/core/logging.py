"""Configuración de logging de la aplicación.

Sin esto, bajo uvicorn los loggers `app.*` propagan a un root sin handler: cada
`logger.info` se descartaba en silencio y los avisos salían sin marca de tiempo
ni nivel. Eso incluía la telemetría del gateway de IA y la traza de auditoría de
Brave/Hermes. Ver ADR-032.

Regla dura del proyecto que esta configuración no debe romper: solo se registran
metadatos (runtime, modelo, motivo de parada, recuento de tokens); nunca el
contenido de conversaciones, consultas ni resultados. Por eso el formato no
incluye cuerpos de petición ni respuesta.
"""

import logging
from logging.config import dictConfig

from app.core.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    dictConfig(
        {
            "version": 1,
            # Los loggers de uvicorn ya existen cuando arranca la aplicación.
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": LOG_FORMAT,
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {
                "handlers": ["stderr"],
                "level": settings.log_level,
            },
            "loggers": {
                # El log de acceso lo emite el proxy inverso; duplicarlo aquí
                # solo añade ruido y una copia más de rutas con identificadores.
                "uvicorn.access": {
                    "handlers": ["stderr"],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
        }
    )
    logging.captureWarnings(True)
