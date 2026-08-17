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

# El proceso no necesita privilegios: solo lee su código y escribe en el
# volumen de documentos. Se crea el punto de montaje ya con el dueño correcto
# para que un volumen nuevo herede la propiedad al crearse; un volumen que YA
# existía sigue siendo de root y hay que corregirlo una vez a mano (ver
# docs/despliegue.md).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /var/lib/asistente_ayuntamientos/documents \
    && chown -R app:app /var/lib/asistente_ayuntamientos /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
