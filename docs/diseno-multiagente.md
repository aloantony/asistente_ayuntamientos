# Especificación cerrada: estructura multi-agente del asistente IA

Documento de traspaso recibido el 2026-06-12 desde la sesión de diseño multi-agente (diseño dialogado + revisión adversarial con 69 agentes: 36 hallazgos confirmados, 4 refutados). Estado: **especificación de referencia; memoria controlada y Hermes Agent actualizados el 2026-06-15**. Transcripción del resumen final; los pasajes que llegaron corruptos en la copia se han reconstruido de forma conservadora a partir del contexto del repositorio y están marcados con ⟦r⟧.

Proyecto: asistente_ayuntamientos (FastAPI + Next.js, multi-tenant, RBAC usuario→grupo→rol→permiso con bypass `is_superuser`). Misión inmediata: el primer usuario real es un alcalde que dictará los requisitos del producto conversando con el asistente (ADR-009). Hoy existe un único agente de intake: bucle síncrono de tool-use en `backend/app/assistant/service.py`, 7 herramientas en `backend/app/assistant/tools.py`, gateway de salida único en `backend/app/assistant/gateway.py` (ADR-013). Nada se reemplaza del diseño original; 6 de 9 decisiones se ajustan.

## Decisiones finales (D1–D9)

**D1 — Catálogo único de herramientas.** Sustituir `TOOL_DEFINITIONS`/`_EXECUTORS` por un `TOOL_CATALOG` de dataclasses congeladas `ToolSpec {name, schema, executor, read_only, label (español), domain ∈ {requirements, projects, organizations, memory, documents}}`. Serialización determinista (estable para prompt caching). Exponer `{name, label, read_only, domain}` en `GET /assistant/status` y eliminar los espejos hardcodeados del frontend (`MUTATING_TOOLS` en `app/lib/useAssistantController.ts:23-27` y `ASSISTANT_TOOL_LABELS` en `app/components/types.ts:718-726`).

**D2 — Registro declarativo de agentes.** Nuevo `backend/app/assistant/agents.py`: `AgentSpec` congelado `{key, name, description, objective, tool_names frozenset, required_permission, model, max_iterations}` + `AGENT_REGISTRY` + aserciones en import (todo `tool_names` ⊆ catálogo). Los códigos de permiso `assistant.*` se siembran desde el registro. `execute_tool` gana un parámetro obligatorio `allowed: frozenset[str]` y rechaza con "Herramienta no disponible para este agente": el techo de herramientas se aplica en código, nunca solo en el prompt.

**D3 — Permisos en dos capas.** Capa agente: chequeo global de permiso (patrón de `assistant/routes.py:31-38`), `get_allowed_agents(db, user)` con bypass explícito de superusuario; conjunto vacío → 403 con mensaje en español antes de tocar el gateway. Capa herramienta: los ejecutores siguen reutilizando el RBAC por organización de las rutas REST (sin cambios). **Decisión por defecto aplicada** (pregunta 4 sin respuesta): el discusor queda gobernado por `assistant.use`, sin código propio — quien puede abrir el chat siempre tiene al menos un agente; evita la trampa del despliegue (la siembra inserta códigos pero no los asigna a ningún rol → todos sin agentes, enmascarado al probar como superusuario). Obligatorio: 4 tests de regresión con usuarios no superusuarios y nota de onboarding de permisos.

**D4 — Router por mensaje (elección firme del usuario).** Enrutado pegajoso: continuar con el agente del último mensaje salvo cambio claro de tarea; entrada = últimos 4-6 mensajes anotados + agentes permitidos del usuario. Política asimétrica: por defecto discusor; consulta solo ante petición claramente de lectura. Atajos sin LLM: un solo agente permitido → directo; mensaje corto (~<20 chars, p. ej. "sí") con `agent_key` previo → mismo agente (los turnos de confirmación del flujo borrador→enviado no deben pasar por el router). Router = modelo pequeño (`settings.assistant_router_model`, candidato `claude-haiku-4-5`), `max_tokens` ≤ 64, sin thinking, `tool_choice` forzado a una herramienta `route` con enum cerrado de los agentes permitidos. Cascada de fallback que **nunca** sale del conjunto permitido (prioridad discusor > consulta > documentos). Migración en `assistant_messages`: `agent_key VARCHAR(100) NULL` + `routing JSON {candidates, chosen, source: router|fallback|shortcut, fallback_reason}`. Desde el primer commit: prompt caching (`cache_control`; relecturas a 0,1×, palanca de coste dominante con hasta ~10 llamadas/turno), aviso por `input_tokens` altos, manejo del 400 por contexto excedido, y excluir turnos de error de `build_history` (hoy una conversación larga se "brickea" sola: cada reintento fallido persiste otro mensaje). Ventana de recencia: diferida, con anclaje de referentes ("#12 — Alumbrado calle Mayor") cuando se haga. UI: una sola persona "Asistente", `agent_key` visible de forma discreta. Streaming = palanca principal de latencia percibida.

**D5 — Memoria persistente por organización** (lo más corregido por la revisión). Tabla `assistant_memory_entries {organization_id FK, category, content, status, created_by_id, procedencia conversation_id/message_id}`. Nunca un blob de texto: filas granulares tipadas (lo más defendible ante RGPD según EDPB/AEPD: borrado por entrada, procedencia, minimización). La decisión final aplicada es memoria controlada: el agente no escribe memoria reusable directamente, solo propone entradas; el backend comprueba permisos, guarda la propuesta como `proposed`, y un responsable con `assistant.memory.review` puede aprobar, editar, rechazar, archivar o bloquear. Solo las entradas `approved` se inyectan en turnos futuros, siempre como contexto delimitado y con permiso `assistant.memory.view` en la organización. La memoria oficial reside en PostgreSQL, no en el runtime de IA.

**D6 — Lineup v1.** Discusor y memorizador de requisitos (el agente actual renombrado + memoria) y consulta (solo lectura; test que verifique que todas sus herramientas son `read_only`). **Decisión por defecto aplicada** (pregunta 1: indiferente): documentos queda fuera de v1 → v1.1, con go/no-go el 2026-07-01 y prerrequisitos: migración de `title`/`description` en `Document` (hoy solo hay `original_filename`, puesto por usuarios y probable portador de datos personales), cambios en la API/formulario de subida, y corpus real. No sembrar ni registrar su permiso hasta entonces. Si entra: una sola herramienta `list_documents` de solo lectura reutilizando el camino probado de `documents/routes.py`, matching en servidor, top 10-20 resultados. Criterio de fusión discusor/consulta acordado ya: si la matriz de confusión del router (20-30 frases en español por agente, junto a la evaluación de ADR-013) muestra confusión persistente, se fusionan.

**D7 — Hermes Agent: se acepta solo como runtime privado.** Hermes Agent es una aplicación/infraestructura de agente, no el modelo ni la memoria institucional oficial. Puede ejecutarse localmente o en un servidor privado y exponerse mediante su API Server compatible con OpenAI; el backend lo consume con `ASSISTANT_RUNTIME=hermes_agent`. No se delegan en Hermes Agent RBAC multi-tenant, auditoría, revisión humana ni gobierno de memoria: todo eso sigue en esta aplicación. El gateway mantiene la minimización y es el único punto de salida.

**D8 — Skills automejorables: diferidas** con 4 señales medibles de adopción y umbrales numéricos en el ADR: (1) churn del system prompt, (2) secuencias de herramientas repetidas en los rastros `actions` de los mensajes, (3) correcciones humanas con patrón, (4) ratio factual/procedimental en las entradas de memoria. La columna `routing` alimenta la señal 2.

**D9 — Filtro de contenido sensible (Mistral UE): aparcado** (decisión firme del usuario; la prioridad es la recogida profunda de requisitos). Revisión: 2026-07-01 o antes de dar acceso a usuarios externos, lo que llegue primero. Documentar como "relajación temporal de minimización" y enmendar `docs/requisitos.md` línea 33 (hoy promete no enviar datos municipales sensibles a IA externa, lo que D5/D6 contradicen) y el README. Si el filtro se retoma: investigación propia (paradoja: la llamada de filtrado envía fuera justo el contenido sensible) y siempre a través de `gateway.py`.

## Respuestas del usuario a las preguntas abiertas (2026-06-12)

1. Documentos en v1: indiferente → aplicado v1.1 (arriba).
2. Memoria efecto inmediato vs confirmación: "pronto para decidir" → aplicado efecto inmediato v1 + revisión obligatoria antes de usuarios externos.
3. Dictado por voz: **APARCADO**. No invertir más en ADR-012 ni en el código de voz ⟦r⟧; la entrada será por teclado de momento.
4. Gobierno del discusor: sin respuesta → aplicado default: gobernado por `assistant.use` ⟦r⟧.

## Orden de implementación (los 4 primeros son imprescindibles) ⟦r⟧

1. Refactor base: `ToolSpec`/`TOOL_CATALOG` + `AgentSpec`/`AGENT_REGISTRY` + `allowed` en `execute_tool` (D1, D2).
2. Guarda borrador→enviado a código: hoy vive solo en el system prompt ⟦r⟧; el paso a `submitted` debe exigir confirmación humana explícita fuera del canal del modelo.
3. Migración: `organization_id` en `assistant_conversations` + `agent_key`/`routing` en `assistant_messages` (formato `YYYYMMDD_NNNN_description.py`, con `upgrade()` y `downgrade()`).
4. Permisos: siembra desde el registro, `get_allowed_agents`, 4 tests de regresión con usuarios no superusuarios ⟦r⟧.
5. Router con pegajosidad, atajos y fallback dentro del conjunto permitido (D4) ⟦r⟧.
6. Memoria (D5): tabla, herramientas, inyección, UI ⟦r⟧.
7. Evaluación: matriz de confusión del router junto a la evaluación de proveedor de ADR-013 ⟦r⟧.

## Documentación asociada

- ADR-015: estructura multi-agente (registro declarativo, router, permisos en dos capas, Hermes Agent solo como runtime privado) ⟦r⟧.
- ADR-016: memoria organizacional persistente controlada (propuesta por agente, aprobación humana, estado bloqueada) ⟦r⟧.
- ADR-017: relajación temporal de minimización (D9) + criterios medibles de adopción de skills (D8) con umbrales y fechas de revisión.
- Apéndice a ADR-013: evaluación en dos pistas (evaluación de proveedor de ADR-013 + matriz de confusión del router) ⟦r⟧.
- Actualizar: `docs/requisitos.md` (línea 33), `docs/arquitectura.md` §IA, `README.md`, docstring de `gateway.py`. Docs en español, commits de docs separados de los de feature.

## Reglas duras que esta implementación debe respetar ⟦r⟧

Todo egress de IA solo por `gateway.py` (cambio de runtime = gateway + configuración; `ASSISTANT_RUNTIME`, `ASSISTANT_MODEL` y `HERMES_AGENT_*` por variables de entorno); nunca enviar documentos originales ni datos municipales almacenados, logs solo con metadatos; tests del asistente sin llamar a la API real (override de `get_gateway` vía `dependency_overrides`, ver `tests/test_assistant.py`); nuevos endpoints con tests de tenancy y permisos; archivar-no-borrar con las excepciones de memoria documentadas; sin tooling nuevo sin ADR; UI en español, errores traducidos en `frontend/app/lib/api.ts`.

## Fuentes (las URL llegaron parcialmente corruptas; se listan las identificables) ⟦r⟧

- Building Effective Agents — Anthropic (patrón "routing" del router por mensaje).
- Prompt caching — Claude Docs (platform.claude.com/docs/en/build-with-claude/prompt-caching).
- Memory tool — Claude Docs.
- OpenAI Agents SDK — Handoffs; Mistral Agents API — Handoffs.
- EDPB — AI Privacy Risks & Mitigations in LLMs (2025).
- AEPD — Orientaciones sobre IA agéntica (2026).
- Art. 32 LOPDGDD (bloqueo de datos).
