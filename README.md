# Asistente Ayuntamientos

Base técnica inicial para una plataforma privada de asistencia a trabajo municipal y administrativo.

## Stack

- Backend: FastAPI con Python
- Frontend: Next.js
- Base de datos: PostgreSQL
- Cache/cola futura: Redis
- Despliegue local: Docker Compose

## Requisitos

- Docker
- Docker Compose

## Configuración

Copia el archivo de ejemplo de variables de entorno:

```bash
cp .env.example .env
```

El archivo `.env.example` no contiene credenciales reales. Para desarrollo local usa valores de ejemplo.

## Ejecución

Construye y arranca los servicios:

```bash
docker compose up --build
```

Servicios disponibles:

- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- Health check: http://localhost:8000/health

## Comprobación rápida

```bash
curl http://localhost:8000/health
```

Respuesta esperada:

```json
{"status":"ok"}
```

## Alcance actual

Esta base no incluye autenticación, modelos de base de datos, agentes de IA, integraciones externas, scraping, QGIS, Hermes ni generación documental.
