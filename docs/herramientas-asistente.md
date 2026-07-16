# Herramientas de Anacleto: alcance y hoja de ruta

Estado de referencia: 2026-07-16.

## Objetivo

Anacleto debe ofrecer, dentro del producto municipal, las capacidades públicas
que el usuario reconoce en una sesión moderna de ChatGPT. No se pretende copiar
la interfaz privada de ChatGPT ni delegar en ella la aplicación: PostgreSQL,
RBAC, tenancy, memoria, confirmaciones, auditoría y retención continúan siendo
responsabilidad de este backend.

La paridad se mide por la tarea que el usuario puede completar, no por que la
implementación use exactamente el mismo proveedor. Las capacidades privadas o
sin una API pública equivalente se sustituirán por componentes propios y
aislados.

## Matriz de capacidades

| Capacidad visible | Estado | Implementación o siguiente entrega |
| --- | --- | --- |
| Conversación con streaming y herramientas municipales | Disponible | Motor model-first, SSE, catálogo filtrado por permisos y auditoría propia |
| Búsqueda web | Disponible con configuración | Brave Search, DLP previo, RBAC, límites y fuentes auditadas |
| Lectura de páginas encontradas | Oleada 1 | Solo HTTP(S), procedencia búsqueda→lectura, defensa SSRF, proceso limitado y contenido marcado como no confiable |
| Adjuntar contenido a un turno | Oleada 1 | TXT UTF-8 acotado; imágenes como vista previa/metadatos; formatos estructurados aún no se interpretan |
| Acciones con efectos | Oleada 1 | Política declarativa, autorización de un solo uso ligada al contenido y resultado durable e idempotente |
| Voz por turnos y Realtime | Parcial | STT/TTS y Realtime bajo configuración; falta completar la experiencia multimodal de producción |
| Memoria controlada | Parcial | Memoria municipal revisable; falta personalización más amplia y políticas de caducidad por categoría |
| Análisis de PDF, Office y datos | Planificada, oleada 2 | Servicio aislado de extracción y sandbox de Python sin red ni secretos, con cuotas de CPU, RAM, disco y tiempo |
| Generación y edición de imágenes | Planificada, oleada 2 | Adaptador de proveedor, almacenamiento temporal, moderación, procedencia y controles de coste |
| Apps y conectores | Planificada, oleada 3 | MCP/conectores con OAuth, bóveda de secretos, scopes por usuario y confirmación separada para escrituras |
| Navegación y control de ordenador | Planificada, oleada 4 | Navegador efímero aislado, allowlist de destinos, capturas auditadas y aprobación antes de efectos externos |
| Investigación profunda | Planificada, oleada 4 | Trabajo durable en segundo plano, plan/fuentes, cancelación, checkpoints y reanudación |
| Tareas programadas | Planificada, oleada 4 | Scheduler durable, zona horaria, idempotencia, reintentos y notificaciones configurables |
| Canvas/artefactos editables | Planificada, oleada 4 | Documentos versionados y editables con exportación y trazabilidad |
| Modo de estudio, grabación y experiencias proactivas | En evaluación | Requieren diseño de producto, consentimiento, privacidad y APIs disponibles; no son herramientas intercambiables del modelo |

## Orden de entrega

### Oleada 1: perímetro seguro

- Autorizaciones declarativas para cualquier herramienta con efectos.
- Ejecución durable: el efecto y su comprobante se confirman en la misma
  transacción y una autorización no puede reutilizarse.
- Búsqueda inicial y lectura exclusivamente de las fuentes devueltas por esa
  búsqueda. Después de introducir contenido web no confiable se bloquea
  cualquier otra herramienta durante el turno.
- Adjuntos asociados al mensaje y al usuario. El contenido de un adjunto no
  puede activar herramientas en ese turno.
- TXT estrictamente UTF-8 y acotado como primer formato legible. PDF, DOCX y XLSX
  se declaran no compatibles hasta disponer del sandbox de la oleada 2.

### Oleada 2: creación y análisis aislados

- Modelo común de artefacto, archivo generado, evento de herramienta y coste.
- Adaptador para herramientas alojadas de Responses cuando cumplan los mismos
  controles de permisos, retención y auditoría.
- Sandbox de análisis de datos/código sin red, con sistema de archivos efímero,
  imágenes base fijadas y límites estrictos.
- Generación y edición de imágenes, incluida la entrega segura de resultados.

### Oleada 3: aplicaciones y datos externos

- Cliente MCP y conectores con un registro de servidores aprobado.
- OAuth por usuario, scopes mínimos, cifrado y rotación de secretos.
- Separación explícita entre lectura, preparación y ejecución de acciones
  externas; estas últimas requieren confirmación vinculada al payload final.

### Oleada 4: agentes de larga duración

- Navegador/computer use en una máquina efímera sin acceso a la red interna.
- Investigación profunda y tareas programadas mediante trabajos durables,
  observables, cancelables y reanudables.
- Canvas/artefactos, flujos de estudio y grabación, solo después de definir su
  consentimiento y ciclo de vida de datos.

## Reglas que no se pueden delegar al modelo

Toda herramienta nueva debe cumplir, como mínimo:

1. permiso RBAC y aislamiento de organización aplicados en el ejecutor;
2. esquema y límites de entrada validados en backend;
3. confirmación de un solo uso para efectos, ligada a usuario, conversación,
   mensaje, herramienta y payload canónico;
4. idempotencia, cancelación y resultado terminal durable;
5. egreso de red explícito, protección SSRF y secretos fuera del prompt;
6. contenido web, archivos y respuestas de conectores tratados como datos no
   confiables, nunca como instrucciones;
7. auditoría sin cuerpos sensibles, límites de coste y rate limiting;
8. pruebas de tenancy, carreras, timeout, prompt injection y recuperación;
9. feature flag apagado por defecto hasta completar evaluación jurídica,
   privacidad, operación y observabilidad.

## Criterio de producción

Una capacidad no pasa a `disponible` únicamente porque funcione en la interfaz.
Debe tener pruebas de regresión y abuso, métricas de latencia/error/coste,
runbook, alertas, límites configurables, política de retención y un mecanismo de
desactivación independiente. El runtime transitorio `codex_subscription` sirve
para evaluación local, pero las herramientas de producción deben funcionar con
una API y credenciales de servicio aprobadas.

Referencias oficiales de implementación: [Tools](https://developers.openai.com/api/docs/guides/tools),
[web search](https://developers.openai.com/api/docs/guides/tools-web-search),
[file search](https://developers.openai.com/api/docs/guides/tools-file-search),
[Code Interpreter](https://developers.openai.com/api/docs/guides/tools-code-interpreter),
[image generation](https://developers.openai.com/api/docs/guides/tools-image-generation),
[computer use](https://developers.openai.com/api/docs/guides/tools-computer-use),
[MCP y conectores](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
y [Realtime](https://developers.openai.com/api/docs/guides/realtime).
