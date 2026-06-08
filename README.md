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

## Migraciones

Con los contenedores levantados, aplica las migraciones de Alembic:

```bash
docker compose exec backend alembic upgrade head
```

También puedes ejecutarlas en un contenedor temporal:

```bash
docker compose run --rm backend alembic upgrade head
```

## Comprobación rápida

```bash
curl http://localhost:8000/health
```

Respuesta esperada:

```json
{"status":"ok"}
```

## Crear el primer administrador

Configura `BOOTSTRAP_ADMIN_TOKEN` en `.env` antes de arrancar el backend. El endpoint solo permite crear un superusuario si todavía no existe ningún usuario.

```bash
curl -X POST http://localhost:8000/auth/bootstrap-admin \
  -H "Content-Type: application/json" \
  -H "X-Bootstrap-Admin-Token: dev-bootstrap-token" \
  -d '{"email":"admin@example.com","password":"change-me-strong","full_name":"Admin"}'
```

Cuando ya exista cualquier usuario, este endpoint responderá con error y no creará más administradores iniciales.

## Login

```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"change-me-strong"}'
```

Respuesta esperada:

```json
{"access_token":"...","token_type":"bearer"}
```

Para consultar el usuario autenticado:

```bash
TOKEN="pega-aqui-el-access-token"
curl http://localhost:8000/auth/me \
  -H "Authorization: Bearer $TOKEN"
```

## Alcance actual

Esta base incluye autenticación JWT, usuarios y una primera estructura RBAC simple. No incluye agentes de IA, integraciones externas, scraping, QGIS, Hermes ni generación documental.
