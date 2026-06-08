# Arquitectura

## Visión general

El proyecto parte de una arquitectura simple con servicios separados para backend, frontend, PostgreSQL y Redis.

## Servicios

- `backend`: API HTTP construida con FastAPI.
- `frontend`: aplicación web construida con Next.js.
- `postgres`: base de datos relacional para uso futuro.
- `redis`: servicio preparado para cache o cola de trabajos en fases posteriores.

## Comunicación

El frontend se publica en el puerto `3000` y el backend en el puerto `8000`. El backend expone inicialmente `GET /health`.

## Fuera de alcance en esta fase

No se implementan autenticación, modelos de datos, agentes de IA, integraciones externas, scraping, QGIS, Hermes ni generación de documentos.
