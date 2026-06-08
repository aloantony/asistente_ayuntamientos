# Requisitos

## Requisitos funcionales iniciales

- La API debe exponer `GET /health`.
- El endpoint de salud debe responder con `{"status":"ok"}`.
- La aplicación web debe arrancar como servicio independiente.

## Requisitos técnicos iniciales

- Backend en FastAPI.
- Frontend en Next.js.
- PostgreSQL definido en Docker Compose.
- Redis definido en Docker Compose.
- Configuración mediante variables de entorno.

## Restricciones

- No añadir autenticación todavía.
- No añadir modelos de base de datos todavía.
- No añadir integraciones de IA o APIs externas todavía.
- No incluir secretos ni credenciales reales.
