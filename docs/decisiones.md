# Decisiones técnicas

## ADR-001: Separación backend/frontend

Se mantiene una separación clara entre API FastAPI y aplicación Next.js para permitir evolución independiente de cada parte.

## ADR-002: Docker Compose para desarrollo local

Se usa Docker Compose como entorno local inicial porque permite levantar backend, frontend, PostgreSQL y Redis de forma reproducible.

## ADR-003: Configuración por variables de entorno

El backend lee la configuración desde variables de entorno para evitar valores acoplados al código y facilitar futuros despliegues.

## ADR-004: Alcance mínimo inicial

La primera versión solo contiene la base técnica. No incluye autenticación, modelos, integraciones externas ni automatizaciones avanzadas.
