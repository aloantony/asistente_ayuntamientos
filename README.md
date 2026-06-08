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
docker compose up -d --build
```

Servicios disponibles:

- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- Health check: http://localhost:8000/health

Los puertos de frontend y backend se publican solo en `127.0.0.1`. PostgreSQL y Redis quedan internos dentro de Docker Compose.

Si ejecutas la pila en un servidor remoto, abre la aplicación con túnel SSH:

```bash
ssh -L 3000:localhost:3000 -L 8000:localhost:8000 dev@SERVER_IP
```

Después abre http://localhost:3000 en tu navegador.

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

## Login desde el frontend

La aplicación de Next.js usa `NEXT_PUBLIC_API_BASE_URL` para llamar al backend. En desarrollo local el valor por defecto es:

```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

Para iniciar sesión:

1. Arranca la pila con `docker compose up -d --build`.
2. Aplica migraciones si la base de datos está vacía.
3. Crea el administrador inicial si todavía no existe.
4. Abre http://localhost:3000.
5. Introduce el email y la contraseña del administrador.

El frontend guarda el `access_token` en `localStorage`, consulta `/auth/me` para restaurar la sesión y permite cerrar sesión desde el dashboard básico.

## Login por API

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
