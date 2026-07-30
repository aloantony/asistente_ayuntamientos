FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY healthcheck.py .

# Usuario sin privilegios: hasta ahora la imagen corría como root, lo que además
# dejaba los __pycache__ del repo montado con propiedad root. El directorio de
# documentos se crea aquí con su propiedad para que un volumen nuevo la herede
# al montarse. Ver ADR-032.
#
# Reutilizar un volumen `document_storage` existente exige un chown puntual, que
# está documentado en docs/despliegue.md. Nunca hay que borrar el volumen.
ARG APP_UID=10001
ARG DOCUMENT_STORAGE_ROOT=/var/lib/asistente_ayuntamientos/documents
RUN useradd --system --uid ${APP_UID} --create-home --shell /usr/sbin/nologin app \
    && mkdir -p ${DOCUMENT_STORAGE_ROOT} \
    && chown -R ${APP_UID}:${APP_UID} ${DOCUMENT_STORAGE_ROOT} /app

USER ${APP_UID}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
