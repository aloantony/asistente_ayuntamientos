# Decisiones técnicas

## ADR-001: Separación backend/frontend

Se mantiene una separación clara entre API FastAPI y aplicación Next.js para permitir evolución independiente de cada parte.

## ADR-002: Docker Compose para desarrollo local

Se usa Docker Compose como entorno local inicial porque permite levantar backend, frontend, PostgreSQL y Redis de forma reproducible.

## ADR-003: Configuración por variables de entorno

El backend lee la configuración desde variables de entorno para evitar valores acoplados al código y facilitar futuros despliegues.

## ADR-004: Alcance mínimo inicial

La primera versión mantiene el alcance acotado a la base técnica, autenticación y un modelo RBAC inicial. No incluye integraciones externas ni automatizaciones avanzadas.

## ADR-005: RBAC simple como base inicial

Se usa un modelo RBAC explícito y sencillo: usuarios, grupos, roles y permisos. La pertenencia se encadena como usuario-grupo, grupo-rol y rol-permiso, con un indicador `is_superuser` para el propietario inicial.

Esta decisión evita introducir todavía un motor complejo de políticas. Para la fase inicial necesitamos una estructura fácil de entender, migrar y auditar. La comprobación fina de permisos se podrá añadir después sobre estas tablas cuando existan casos de uso reales.

## ADR-006: Endurecimiento del aislamiento multi-tenant (2026-06-12)

Una auditoría del código detectó vías de escalada entre tenants. Se decide:

- Conceder o retirar `is_superuser` es operación exclusiva de superusuarios (antes bastaba `users.manage`).
- `users.manage` queda delimitado por organización: solo se pueden editar/borrar usuarios que comparten alguna organización donde el administrador tiene el permiso.
- La guarda de "último superusuario activo" se aplica también a PATCH (desactivación/degradación), no solo a DELETE.
- Roles y permisos son objetos globales de plataforma: sus mutaciones quedan reservadas a superusuarios. La asignación grupo-rol sigue delimitada por la organización del grupo.
- Crear organizaciones (tenants nuevos) queda reservado a superusuarios.
- Enlazar un documento a una ordenanza exige que el autor tenga acceso a ese documento; los documentos inaccesibles responden 404 para no filtrar su existencia.

Contexto: el primer usuario externo (alcalde, usuario no superusuario) entra pronto; el modelo anterior asumía operador único de confianza.

## ADR-007: Seeding automático del catálogo de permisos (2026-06-12)

El backend siembra el catálogo de permisos de forma idempotente al arrancar (hook lifespan). Antes requería una llamada manual de superusuario y una base nueva quedaba con la tabla vacía. Si las migraciones no se han aplicado aún, el arranque continúa con un warning para permitir ejecutar Alembic.

## ADR-008: Arnés de tests con pytest en contenedor (2026-06-12)

Los tests de backend corren con pytest dentro del contenedor backend (mismas versiones que producción), contra una base PostgreSQL de test separada. Cada test se envuelve en una transacción externa con savepoints (`join_transaction_mode="create_savepoint"`) y rollback final: los `commit()` del código de aplicación funcionan y ningún test toca datos de desarrollo. Las dependencias de test viven en `requirements-dev.txt` y no entran en la imagen.

Prioridad de cobertura: matriz de permisos, aislamiento entre tenants y reglas de escalada, por ser el código cuya regresión es más cara y silenciosa.

## ADR-009: Agente de IA de intake de requisitos antes que Ordenanzas v1 (2026-06-12)

El primer usuario real es un alcalde que comunicará los requisitos del producto conversando con un agente de IA ("tipo Jarvis"). Se reordena el roadmap: el agente conversacional de intake va antes que Comparación de Ordenanzas v1. Principios: el agente opera con los permisos RBAC del usuario, crea requisitos como borradores supervisables, deja rastro auditable de cada acción, y toda llamada a la API externa de IA pasa por un gateway interno con minimización de datos (sin enviar documentos originales).

## ADR-010: Sesión de navegador con cookie httpOnly (2026-06-12)

El frontend deja de guardar el JWT en localStorage (expuesto a XSS). El login emite una cookie `access_token` httpOnly SameSite=Lax (Secure fuera de desarrollo) y existe `POST /auth/logout` para limpiarla. La cabecera `Authorization: Bearer` sigue aceptada para clientes de API y tests. SameSite=Lax mitiga CSRF en los POST cross-site; frontend y backend deben servirse desde el mismo host (localhost) para compartir la cookie entre puertos. Se añade rate limiting en memoria a `POST /auth/login` (10 intentos/minuto por IP; al escalar a multi-worker deberá moverse a Redis).

## ADR-011: Paginación server-side con X-Total-Count (2026-06-12)

Los listados de municipios, ordenanzas, requisitos y usuarios admin aceptan `limit` (1-200, por defecto 100) y `offset`, y devuelven el total en la cabecera `X-Total-Count` (expuesta vía CORS). Se eligió cabecera en lugar de envolver la respuesta para no romper los contratos existentes. El listado de ordenanzas ya no incluye `text_content`: el texto legal completo solo viaja en el detalle, lo que desbloquea importar el dataset INE (~8.100 municipios) sin respuestas gigantes.

## ADR-012: Dictado por voz solo con reconocimiento local (2026-06-12)

El asistente acepta entrada por voz mediante la Web Speech API **exclusivamente en modo local** (`processLocally = true`, Chrome 139+): el audio no sale del equipo del usuario. El modo nube por defecto del navegador envía el audio a servidores del proveedor sin DPA que cubra ese tratamiento — inaceptable para datos de una administración pública — así que cuando el reconocimiento local no está disponible el botón se deshabilita en lugar de degradar. Alternativa futura con mejor posición RGPD si hace falta cobertura universal: Whisper auto-alojado (ver docs/investigacion-api-ia.md §6).

## ADR-013: Criterio de selección del proveedor de IA (2026-06-12)

La investigación comparativa (docs/investigacion-api-ia.md) concluye: candidato principal Mistral Small 4 (procesamiento UE por defecto, ~$0,10/$0,30 por MTok, sujeto a una evaluación de fiabilidad de tool use con ~50 conversaciones reales); segunda opción Claude Haiku 4.5 vía Bedrock con endpoints UE (tool use más fiable, `strict`, caché). El coste del LLM no es el criterio decisivo a los volúmenes previstos ($75–950/mes en el escenario alto): pesan más la fiabilidad de herramientas y el encaje RGPD/ENS. El gateway mantiene el modelo configurable (`ASSISTANT_MODEL`); la migración de proveedor, si se decide, se limita a `app/assistant/gateway.py`. Reevaluar el 2026-07-01 (subidas de precio UE anunciadas) y al incorporar las cargas futuras de ordenanzas.

## ADR-014: Frontend multi-ruta con shell de navegación (2026-06-12)

La página única con cinco paneles apilados se sustituye por rutas del App Router con una barra lateral cuyos ítems usan los mismos predicados de permisos que gobernaban los paneles: `/asistente`, `/requisitos`, `/proyectos`, `/cuenta` y `/admin/{usuarios,grupos,organizaciones,roles,municipios,ordenanzas}`. Decisiones asociadas:

- La sesión (usuario, embudo de 401, logout) vive en un `SessionProvider` en el layout raíz; el guard es client-side (un `middleware.ts` queda como mejora futura). Tras el login se aterriza en la primera sección visible según permisos (el alcalde, directamente en el asistente).
- Cada ruta monta solo su controlador y carga sus datos al entrar; desaparecen las ~11 peticiones del login y el N+1 de documentos fuera de `/proyectos`. Las cadenas de refresco entre dominios se eliminan: cada ruta recarga al montar.
- El controlador admin monolítico (2.218 líneas, 105 estados) se trocea en seis hooks por dominio (`app/lib/admin/`); las listas de otros dominios llegan por fetchers ligeros (`app/lib/fetchers.ts`).
- Selección, filtros y página viven en la URL (`?id=`, `?c=`, `?q=&page=`): deep-links, refresh y botón atrás funcionan. Los filtros de municipios, ordenanzas y requisitos se aplican en el servidor (cierra la búsqueda incompleta sobre la página de 50 y prepara el dataset INE).
- Sin dependencias nuevas: solo primitivas del App Router y CSS propio (sección "app shell" monocroma).

