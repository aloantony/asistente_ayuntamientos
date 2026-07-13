# Análisis del repositorio frente a la visión de Anacleto

Fecha: 2026-07-13.

## 1. Alcance

La auditoría estática se realizó sobre `origin/main` en el commit `70c8ad3`,
usando la rama aislada `codex/product-definition-20260713`. Se revisaron backend,
frontend, modelos, migraciones, tests, documentación, configuración y despliegue.

También se comprobó la existencia de
`origin/codex/security-hardening-20260710`, siete commits por delante de `main`.
Esa rama contiene CI, rate limiting distribuido, invitaciones, validaciones de
producción, proxy BOP y runtime autoalojado, pero esas mejoras no forman parte de
la línea base auditada mientras no se integren.

La referencia para decidir encaje de producto es `docs/vision-producto.md`.

## 2. Conclusión ejecutiva

No conviene reiniciar el proyecto. Existe una base aprovechable: monolito
modular, PostgreSQL, RBAC, aislamiento por organización, conversación model-first
con streaming y voz, catálogo de herramientas, gateway de IA, proyectos,
documentos, mapa, normativa y una oficina inicial de tareas con eventos.

El desajuste está en la jerarquía y el gobierno del sistema:

- El código trata a Anacleto principalmente como capturador supervisado de
  requisitos sobre una aplicación modular.
- La visión lo sitúa como agente operativo principal de todo el ayuntamiento.
- El código representa varios tenants en una aplicación compartida.
- La visión exige una instancia operativa por ayuntamiento y servicios centrales
  separados.
- El código dispone de permisos CRUD, pero no de cargos, competencias,
  delegaciones, riesgo, identidad delegada ni conservación.
- La proactividad, la auditoría durable y las integraciones municipales reales
  todavía no existen.

La prioridad no debe ser añadir otro módulo o cambiar de modelo de IA. Debe ser
crear el núcleo de gobierno y ejecución sobre el que Anacleto pueda actuar de
forma segura.

## 3. Capacidades que ya aportan valor

- Autenticación y RBAC usuario → grupo → rol → permiso en
  `backend/app/rbac/permissions.py`.
- Aislamiento operativo por `Organization` y helpers de acceso reutilizables.
- Motor conversacional único, streaming SSE e historial en
  `backend/app/assistant/turn.py`.
- Gateway intercambiable Anthropic/Hermes en
  `backend/app/assistant/gateway.py`.
- Catálogo declarativo de 15 herramientas en
  `backend/app/assistant/tools.py`.
- Voz web y canal Telegram con el mismo contexto de usuario.
- Proyectos, documentos, requisitos, mapa, municipios y ordenanzas como
  capacidades municipales iniciales.
- Importación jurídica supervisada, fragmentos citables y búsqueda semántica.
- Tareas, estados, eventos y rutinas iniciales en `backend/app/agent_office/`.
- Cobertura backend relevante de autenticación, tenancy, permisos, documentos,
  requisitos, asistente, oficina de agentes y ordenanzas.

## 4. Hallazgos P0

### 4.1 Falta el modelo de autoridad municipal

`User` solo contiene identidad, actividad y la bandera técnica `is_superuser`
(`backend/app/users/models.py`). No existen cargo, departamento, responsabilidad,
competencia legal ni sustituciones. El prompt solo conoce nombre y organizaciones
del usuario (`backend/app/assistant/prompts.py`).

Sin este modelo Anacleto no puede adaptarse al alcalde, decidir quién es
responsable, resolver instrucciones incompatibles ni justificar por qué puede
ejecutar una acción.

**Mejora:** modelar perfil municipal, unidad organizativa, cargo, competencia,
asignación temporal, delegación y política. RBAC seguirá controlando acceso
técnico; las competencias gobernarán autoridad y propósito.

### 4.2 La confirmación actual no demuestra consentimiento

La guarda de `create_requirement` desbloquea la acción ante cualquier mensaje
posterior si coinciden organización y título
(`backend/app/assistant/guards.py`). Un “no” puede actuar como confirmación y los
cambios en problema, alcance u otros campos no forman parte de la clave. Las demás
mutaciones dependen de permisos y prompt, sin un contrato común de aprobación.

**Mejora inmediata:** exigir una decisión explícita y estructurada, ligada a
usuario, payload completo, política, caducidad y acción concreta. Añadir una
prueba de regresión donde una negativa nunca confirma.

### 4.3 La ejecución puede informar de éxito cuando la herramienta falla

`run_agent_office_task` marca una tarea como completada después de ejecutar el
cuerpo sin propagar necesariamente `ToolResult.ok=false`
(`backend/app/agent_office/service.py`). Esto invalida seguimiento y confianza.

**Mejora inmediata:** un error de herramienta debe producir fallo o bloqueo
tipado; no un resultado completado. Añadir idempotencia antes de reintentar.

### 4.4 La auditoría no es durable ni transaccional

Las mutaciones pueden confirmar su propia transacción antes de que el mensaje
guarde la traza JSON en `assistant_messages.actions`. Una caída intermedia deja
una actuación sin evidencia conversacional. Además, borrar una conversación o un
usuario puede borrar mensajes en cascada.

**Mejora:** crear un ledger de acciones append-only con actor técnico, delegante,
mandato, política evaluada, riesgo, entrada normalizada, fuente, proveedor/modelo,
aprobaciones, resultado, evidencia e identificador de correlación. La mutación y
su registro deben compartir una frontera transaccional o un outbox fiable.

### 4.5 La línea de integración no contiene el hardening y CI ya preparados

`origin/main` no tiene `.github/workflows/ci.yml`. La rama remota de hardening sí
lo tiene junto con cambios importantes y casi doce mil líneas de diferencia. No
debe iniciarse una reorientación grande sobre ramas divergentes.

**Mejora inmediata:** revisar esa rama por commits, resolver qué cambios siguen
siendo válidos y llevarlos a la rama de integración mediante PRs pequeños. El
runtime autoalojado queda disponible como trabajo futuro, no como prioridad de
producto actual.

### 4.6 El acceso principal al chat pierde la consulta

El inicio envía `?q=` a `/asistente`; la página carga ese texto como borrador,
pero crear la primera conversación lo borra antes de enviarlo
(`frontend/app/(app)/page.tsx`,
`frontend/app/lib/useAssistantController.ts`). El flujo principal anunciado no
completa su acción.

**Mejora inmediata:** conservar y enviar el borrador al crear la conversación y
añadir una prueba del recorrido inicio → nueva conversación → respuesta.

## 5. Hallazgos P1

### 5.1 El modelo de despliegue sigue siendo multi-tenant compartido

Un usuario puede pertenecer a varias organizaciones y `is_superuser` puentea los
límites. No existe identidad de instancia, control plane, heartbeat, canal de
actualización ni soporte temporal. El superusuario actual mezcla administración
municipal y operación de plataforma.

**Mejora:** mantener inicialmente `organization_id` como defensa y compatibilidad,
pero limitar cada despliegue a un ayuntamiento activo. Separar administrador de
instancia, operador de plataforma y soporte temporal. El operador central no debe
ser un usuario con bypass permanente dentro de todas las bases municipales.

### 5.2 La proactividad es nominal

La oficina almacena `scheduled_for` y `next_run_at`, pero no hay scheduler ni
detector de eventos. La API se identifica como `rq/manual-trigger-v1` y la rutina
diaria se dispara manualmente. El frontend tampoco presenta una bandeja de
trabajo iniciado por Anacleto.

**Mejora:** evolucionar `agent_office` a un motor durable de eventos y trabajo con
scheduler, prioridades, dependencias, reintentos, bloqueos, seguimiento y
notificaciones.

### 5.3 No existe una política de autonomía

Los permisos CRUD y las etiquetas de aprobación no expresan propósito, mandato,
vigencia, destinatarios, riesgo, reversibilidad, presupuesto ni acciones
reservadas. Ocho “departamentos de agentes” codificados fijan capacidades
históricas como si fueran la estructura definitiva.

**Mejora:** definir acciones con metadatos de riesgo y precondiciones; evaluar
política de producto, política municipal, competencia y delegación antes de cada
ejecución. Los departamentos pueden sobrevivir como agrupaciones internas, no
como límite del producto.

### 5.4 La memoria es demasiado gruesa

La memoria actual pertenece a una organización, tiene categoría y sensibilidad,
y se aprueba o rechaza. No representa ámbito personal, departamental, de
expediente o municipal, ACL, consentimiento contextual, vigencia ni retención.
Hasta 30 entradas aprobadas se inyectan completas mediante un permiso general.

En frontend se cargan propuestas de memoria, pero no se muestran en una
superficie útil.

**Mejora:** entradas granulares con ámbito, audiencia, procedencia, finalidad,
vigencia, consentimiento, política de retención y permisos por recurso. Recuperar
solo contexto pertinente para el turno.

### 5.5 El gateway adapta proveedores, pero no aplica la política acordada

Hay un runtime global por variables de entorno. No existe registro de proveedores
aprobados, finalidades, clases de datos, recibos de egreso ni política por
ayuntamiento. El contrato actual impide enviar documentos originales, mientras
que la visión permite datos necesarios a proveedores aprobados.

**Mejora:** conservar el gateway y añadir registro de proveedor, política de
egreso, minimización, recibo auditable y bloqueo por categoría. Mantener las
restricciones actuales hasta que controles y contratos estén operativos.

### 5.6 Faltan conectores municipales

Las integraciones existentes son IA, voz, Telegram, web y fuentes jurídicas. No
hay framework para APIs municipales, automatización de interfaces, almacén de
credenciales, identidad delegada, catálogo de acciones ni fuente oficial.

**Mejora:** contrato de conector con acciones declaradas, esquema de entrada y
salida, fuente, riesgo, permisos, idempotencia, evidencias y estado de salud. API
primero; automatización visual solo como alternativa gobernada.

### 5.7 La UI sigue siendo modular y reactiva

Anacleto aparece como sección `BETA` dentro de “Inteligencia”, después de varias
áreas funcionales. El inicio muestra conteos de módulos, no trabajo prioritario.
No hay bandejas de decisiones, bloqueos, aprobaciones, delegaciones o resúmenes.

`AssistantAction` solo representa herramienta, entrada, resultado y éxito. La UI
muestra JSON técnico, pero no mandato, actor, riesgo, fuentes, reversibilidad,
aprobación o evidencia.

**Mejora:** hacer Anacleto persistente y convertir el inicio en briefing/inbox.
Abrir proyectos, mapa, documentos y requisitos como paneles contextuales. Mostrar
tarjetas operativas para usuarios y reservar la traza técnica a la auditoría.

### 5.8 Requisitos todavía no implementa descubrimiento colaborativo

El modelo admite contenido estructurado e hilo de mensajes, pero no participantes,
entrevistas, posiciones discrepantes, responsable competente ni validación final
de una propuesta construida mediante diálogo.

**Mejora:** convertir el requisito en el resultado de una sesión de descubrimiento
con participantes, afirmaciones, decisiones, objeciones y resumen confirmado.

### 5.9 No existe la red de mejora entre instancias

Feedback y funcionalidades transversales se almacenan localmente y enlazan datos
de la misma base. No hay paquete anonimizado, outbox, revisión central,
distribución versionada ni registro local de lo compartido.

**Mejora:** definir un contrato de exportación sin texto bruto por defecto,
depuración local, cuarentena cuando no pueda anonimizarse, revisión del
desarrollador y paquetes de mejora firmados y reversibles.

### 5.10 Faltan conservación y operación de producción

No hay políticas de retención, bloqueos legales, borrado programado, copias y
restauración documentadas, gestor de secretos, TLS, monitorización o health check
real de dependencias. El Compose actual es un entorno de desarrollo.

**Mejora:** diseñar despliegue, recuperación, rotación de secretos, observabilidad
y conservación antes de manejar datos municipales reales.

## 6. Clasificación del código actual

| Área | Decisión | Motivo |
| --- | --- | --- |
| FastAPI, PostgreSQL, Redis/RQ y monolito modular | Conservar | Base suficiente para la siguiente etapa. |
| Autenticación y helpers RBAC | Conservar y ampliar | Buen control técnico; faltan competencias y delegaciones. |
| Conversación, streaming, voz y Telegram | Conservar | Son la entrada principal acordada. |
| Gateway y catálogo de herramientas | Conservar y ampliar | Son las fronteras correctas para proveedor y acciones. |
| Proyectos, documentos, mapa y ordenanzas | Conservar como capacidades | Deben abrirse desde Anacleto, no gobernar la navegación. |
| Pipeline jurídico supervisado | Conservar | Aporta fuentes y revisión; necesita frontera editorial federada. |
| `Organization` como tenant | Adaptar | Una instancia tendrá un ayuntamiento; mantener el scope inicialmente evita una migración destructiva. |
| `is_superuser` | Sustituir gradualmente | Mezcla administrador municipal y operador de plataforma. |
| Prompt centrado en requisitos | Sustituir | El contrato debe describir al agente operativo y su política. |
| Memoria aprobada por organización | Rediseñar | Faltan ámbitos, ACL, consentimiento, vigencia y retención. |
| Oficina de agentes | Rediseñar | Es la semilla del motor de trabajo, pero no es durable ni proactiva. |
| Requisitos | Adaptar | Debe nacer de descubrimiento conversacional y colaborativo. |
| Funcionalidades transversales y feedback | Adaptar | Deben cruzar instancias mediante paquetes depurados, no relaciones locales. |
| Dashboard de conteos y navegación modular | Sustituir | El centro debe ser briefing, conversación y trabajo pendiente. |
| Administración de organizaciones/municipios en la instancia | Ocultar o reinterpretar | La plataforma central gestiona instancias; el municipio gestiona su estructura. |
| JSON técnico visible como supervisión | Mover | La interfaz operativa necesita impacto y decisiones; el JSON pertenece a auditoría. |
| Asistente ciudadano compartiendo contexto interno | No construir | En el futuro será una frontera separada. |

## 7. Arquitectura objetivo por capacidades

El siguiente núcleo no debe organizarse por “los tres casos más frecuentes”,
sino por capacidades reutilizables:

1. **Identidad organizativa:** instancia, persona, unidad, cargo, competencia y
   asignación.
2. **Gobierno:** política, delegación, riesgo, consentimiento y aprobación.
3. **Trabajo:** objetivo, evento, plan, tarea, dependencia, prioridad y estado.
4. **Actuación:** herramienta/conector, intento, idempotencia, evidencia, error y
   reversión.
5. **Conocimiento:** fuente, vigencia, ámbito, memoria, ACL y retención.
6. **Colaboración:** conversación, participante, comunicación, decisión,
   objeción y escalado.
7. **Atención:** notificación, briefing, bloqueo y resumen.
8. **Plataforma:** identidad de instancia, despliegue, salud, soporte temporal,
   exportación depurada y actualización versionada.

Los flujos municipales concretos probarán combinaciones de estas capacidades.

## 8. Orden recomendado de trabajo

### Fase 0: estabilizar la línea base

1. Consolidar en PRs pequeños los commits válidos de seguridad/CI pendientes.
2. Corregir la pérdida de `?q=`, la confirmación falsa y el éxito falso de tareas.
3. Añadir regresiones para esos tres defectos.
4. Mantener esta visión como autoridad y marcar documentos históricos.

### Fase 1: núcleo de gobierno y auditoría

1. Escribir ADRs de instancia, autoridad, autonomía, memoria y egreso de IA.
2. Añadir perfil de instancia, estructura, cargos, competencias y delegaciones.
3. Definir acciones tipadas con riesgo y requisitos de aprobación.
4. Crear el ledger de acciones y consentimientos explícitos.
5. Separar administrador municipal, operador de plataforma y soporte.

### Fase 2: trabajo proactivo

1. Generalizar `agent_office` a eventos, planes, tareas y acciones durables.
2. Incorporar scheduler, outbox, reintentos, idempotencia y dependencias.
3. Implementar prioridad organizacional, bloqueos y seguimiento.
4. Añadir notificaciones inmediatas y resúmenes.

### Fase 3: memoria y experiencia principal

1. Migrar memoria a ámbitos y ACL con consentimiento contextual.
2. Hacer Anacleto persistente en la interfaz.
3. Sustituir el dashboard por briefing, decisiones, trabajo y bloqueos.
4. Presentar documentos, mapa, proyectos y requisitos como vistas contextuales.
5. Añadir pruebas frontend de componentes y recorridos críticos.

### Fase 4: sistemas oficiales

1. Crear SDK/contrato de conectores y almacén de credenciales de instancia.
2. Implementar primero conectores API con fuentes y evidencias.
3. Añadir automatización visual aislada solo donde no exista API.
4. Probar identidad delegada, confirmaciones y recuperación ante fallos.

### Fase 5: plataforma e inteligencia compartida

1. Construir control plane sin acceso ordinario al contenido.
2. Añadir soporte temporal auditado.
3. Implementar exportación automática de aprendizajes anonimizados.
4. Crear revisión del desarrollador y distribución versionada/reversible.
5. Definir la publicación voluntaria de conocimiento municipal reutilizable.

### Fase 6: expansiones diferidas

- Modelos propios detrás del gateway existente.
- Asistente ciudadano separado.
- Sustitución selectiva de sistemas municipales cuando aporte valor demostrado.

## 9. Estrategia de pruebas

- Matriz cargo × competencia × delegación × acción × riesgo.
- Pruebas negativas de aislamiento, conflicto de autoridad y consentimiento.
- Propiedades de idempotencia, reintento y correlación ledger/mutación.
- ACL y retención por ámbito de memoria.
- Contratos de conectores con simuladores; ninguna llamada real en tests.
- Evaluaciones conversacionales amplias por capacidad, no solo por módulo.
- Recorridos frontend de conversación, aprobación, bloqueo y briefing.
- Pruebas de exportación que demuestren ausencia de datos prohibidos.
- Recuperación de copia, rotación de secretos y despliegue gradual.

## 10. Trabajo que no conviene priorizar ahora

- Nuevos módulos CRUD aislados.
- Más departamentos o personajes de agentes codificados.
- Rediseño visual sin contrato de trabajo/autonomía.
- Autoalojar modelos antes de resolver gobierno, acciones y auditoría.
- Atención ciudadana dentro de la instancia interna.
- Migrar o renombrar masivamente `Organization` antes de tener la frontera de
  instancia definida.
