# Runtime local con suscripción Codex

Actualizado: 2026-07-17.

## Alcance

`ASSISTANT_RUNTIME=codex_subscription` permite evaluar Anacleto en desarrollo
con una suscripción ChatGPT que incluya Codex, usando la interfaz oficial
[`codex app-server`](https://learn.chatgpt.com/docs/app-server). No conecta la
web de ChatGPT, no obtiene una clave de API y no sustituye el backend del
producto. Conversaciones, tenancy, permisos, confirmaciones, auditoría, memoria
y herramientas siguen siendo responsabilidad de esta aplicación.

Es una vía transitoria para probar flujos antes de contratar el runtime de
producción. La configuración se rechaza al arrancar si `ENVIRONMENT` no es
exactamente `development`.

## Flujo implementado

1. El gateway arranca un proceso local `codex app-server` y crea un thread
   efímero en un directorio vacío.
2. Publica las herramientas permitidas para el usuario como `dynamicTools`, con
   nombres internos prefijados para no colisionar con herramientas de Codex.
3. Si Codex solicita una herramienta, el proceso queda pausado y el gateway
   devuelve un `AIToolUseBlock` junto a un handle aleatorio que solo vive en
   memoria durante el turno.
4. `turn.py` ejecuta la llamada con el flujo habitual: RBAC, tenancy,
   confirmación de escrituras, límites de rondas/llamadas, auditoría y, para web,
   la fachada Brave configurada.
5. El gateway devuelve el resultado al mismo request `item/tool/call`, continúa
   el turno de Codex y destruye el proceso al terminar, fallar, caducar o
   cancelarse el cliente.

La API `dynamicTools` es experimental y requiere
`capabilities.experimentalApi=true`. Este bridge se ha implementado y validado
contra `codex-cli 0.144.4`; no actualices el CLI sin volver a ejecutar las
pruebas de protocolo y los flujos completos del asistente.

En el entorno local del 2026-07-17, `codex-cli 0.144.5` superó el health check y
una finalización real mínima mediante `app-server`; esto acredita el smoke test,
no sustituye la suite de protocolo completa fijada todavía a `0.144.4`.

## Preparación local

Instala la versión validada y crea un hogar exclusivo. No reutilices ni copies
`~/.codex`: ese directorio puede contener configuración, plugins, MCP, skills e
historial del desarrollador.

```bash
npm install --global @openai/codex@0.144.4
codex --version

export MUNICIPAL_CODEX_HOME="$HOME/.codex-asistente-ayuntamientos"
install -d -m 700 "$MUNICIPAL_CODEX_HOME"
HOME="$MUNICIPAL_CODEX_HOME" CODEX_HOME="$MUNICIPAL_CODEX_HOME" codex login
HOME="$MUNICIPAL_CODEX_HOME" CODEX_HOME="$MUNICIPAL_CODEX_HOME" codex login status
test -f "$MUNICIPAL_CODEX_HOME/auth.json"
test ! -L "$MUNICIPAL_CODEX_HOME/auth.json"
chmod 600 "$MUNICIPAL_CODEX_HOME/auth.json"
```

El login debe informar que usa ChatGPT. El runtime rechaza una cuenta de tipo
API key o Bedrock para impedir cargos accidentales por otra credencial. Además,
`auth.json` debe ser un archivo regular, no un enlace simbólico, y tener permisos
`0600`; el directorio dedicado debe conservar `0700`.

Configura el backend:

```dotenv
ENVIRONMENT=development
ASSISTANT_RUNTIME=codex_subscription
CODEX_SUBSCRIPTION_ENABLED=true
CODEX_SUBSCRIPTION_REAL_DATA_ALLOWED=true
CODEX_SUBSCRIPTION_COMMAND=codex
CODEX_SUBSCRIPTION_HOME=/home/usuario/.codex-asistente-ayuntamientos
CODEX_SUBSCRIPTION_MODEL=
CODEX_SUBSCRIPTION_REASONING_EFFORT=medium
CODEX_SUBSCRIPTION_SESSION_TTL_SECONDS=180
CODEX_SUBSCRIPTION_MAX_SESSIONS=4
CODEX_SUBSCRIPTION_HEALTH_TIMEOUT_SECONDS=3
```

Los dos opt-in quedan en `false` por defecto. El backend exige ambos: el primero
habilita explícitamente el bridge experimental y el segundo reconoce que todo
texto introducido puede enviarse a la cuenta ChatGPT configurada. Activarlos no
sustituye una revisión contractual ni autoriza por sí solo datos personales;
usa datos sintéticos hasta que esa revisión esté cerrada.

Un modelo vacío deja que la suscripción elija su modelo por defecto. Fijar un
slug mejora la reproducibilidad, pero también aumenta el riesgo de volver a ver
«Selected model is at capacity» si ese modelo se satura o deja de estar
disponible.

El binario y el `CODEX_SUBSCRIPTION_HOME` deben existir dentro del mismo entorno
de ejecución que el backend. La imagen Docker estándar no incluye Codex ni debe
montar el `~/.codex` personal. El sidecar/socket descrito como posible evolución
todavía **no está implementado** y `app-server` no debe exponerse por una
interfaz de red pública. Se puede ejecutar el backend en el host o añadir el
override local y explícito descrito más abajo; el Compose base continúa sin
credenciales ni binarios del host.

### Arranque ejecutable con backend en el host

Desde la raíz del repositorio, prepara `.env`, las dependencias y un directorio
de documentos escribible. El comando de Compose no arranca `backend`, `worker`
ni `frontend`:

```bash
docker compose up -d postgres redis
cp .env.example .env
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r backend/requirements.txt
install -d -m 700 "$HOME/.local/share/asistente-ayuntamientos/documents"
export DOCUMENT_STORAGE_ROOT="$HOME/.local/share/asistente-ayuntamientos/documents"
```

Edita `.env` y aplica la configuración Codex anterior. La variable exportada
prevalece sobre el valor Docker de `.env.example`. En la misma terminal ejecuta
la migración y arranca FastAPI:

```bash
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/uvicorn app.main:app --app-dir backend \
  --host 127.0.0.1 --port 8000
```

En otra terminal, instala y arranca el frontend por separado:

```bash
npm --prefix frontend ci
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000 \
  npm --prefix frontend run dev -- --hostname 127.0.0.1 --port 3000
```

No uses únicamente `docker compose up --build` para esta modalidad: el backend
del contenedor estándar no ve ni el binario ni el hogar Codex del host.

### Alternativa local con Docker Compose

El override `docker-compose.codex-subscription.yml` monta exclusivamente la
distribución nativa de Codex y el hogar dedicado; nunca monta `~/.codex`. Ejecuta
backend y worker con el mismo UID/GID propietario de ese hogar, porque el bridge
rechaza credenciales que pertenezcan a otro usuario. Configura además:

```dotenv
CODEX_SUBSCRIPTION_COMMAND=/opt/codex/bin/codex
CODEX_SUBSCRIPTION_HOME=/home/usuario/.codex-asistente-ayuntamientos
CODEX_SUBSCRIPTION_HOST_VENDOR_PATH=/ruta/a/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl
CODEX_SUBSCRIPTION_CONTAINER_UID=1000
CODEX_SUBSCRIPTION_CONTAINER_GID=1000
```

La ruta vendor debe proceder de la misma instalación de Codex que se verificó
en el host y contener `bin/codex`, `codex-path` y `codex-resources`. Antes del
primer arranque, entrega el volumen documental al UID/GID configurado:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.codex-subscription.yml \
  run --rm -T --no-deps --user 0:0 backend \
  chown -R 1000:1000 /var/lib/asistente_ayuntamientos/documents
```

Después reconstruye, migra y arranca siempre con ambos archivos:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.codex-subscription.yml build backend worker frontend
docker compose \
  -f docker-compose.yml \
  -f docker-compose.codex-subscription.yml run --rm -T --no-deps backend \
  alembic -c alembic.ini upgrade head
docker compose \
  -f docker-compose.yml \
  -f docker-compose.codex-subscription.yml up -d backend worker frontend
```

El override es solo para desarrollo local. En producción no deben montarse
tokens OAuth ni usarse credenciales de una suscripción personal.

Comprueba el estado con un usuario que tenga `assistant.use`:

```bash
curl -sS http://127.0.0.1:8000/assistant/status \
  -H "Authorization: Bearer <token>"
```

El estado esperado es `runtime=codex_subscription`, `enabled=true` y
`runtime_healthy=true`. No copies tokens OAuth en `.env`, en Dockerfiles ni en
el repositorio.

## Herramientas web, permisos y voz

La suscripción solo aporta el runtime conversacional. La web interna de Codex
permanece desactivada: para `web_search` configura `WEB_SEARCH_PROVIDER=brave`,
`BRAVE_SEARCH_API_KEY` y concede el permiso RBAC `assistant.web.search` solo a
los usuarios autorizados. Como el backend audita consulta y resultados,
`BRAVE_SEARCH_STORAGE_RIGHTS_CONFIRMED=true` solo puede activarse después de
confirmar que el plan contratado permite ese almacenamiento y cerrar DPA,
retención y tratamiento de nombres o direcciones. Sin clave, derechos y RBAC,
el runtime puede conversar pero no buscar en la web.

La suscripción Codex tampoco incluye voz. Realtime necesita por separado un
proveedor aprobado y su credencial de API (en la integración actual,
`OPENAI_API_KEY` y `ASSISTANT_REALTIME_ENABLED=true`). El fallback requiere sus
propios proveedores: NVIDIA NIM para STT y Azure Speech para TTS. Activar el
bridge no habilita ninguna de estas salidas ni cubre su facturación.

## Aislamiento aplicado

Cada proceso usa `shell=False`, entorno allowlist, `HOME`/`CODEX_HOME`
dedicados, cwd temporal vacío, thread efímero, historial desactivado, sandbox de
solo lectura y política de aprobación `never`. Se desactivan las features de
shell, exec, web, apps, plugins, MCP auxiliares, navegador, computer use,
imágenes, hooks, memoria y multiagente conocidas por el CLI probado. Cualquier
evento de herramienta interna que aun aparezca hace fallar el turno de forma
cerrada. El stderr se drena para evitar bloqueos, pero nunca se registra ni se
incluye en errores.

No existe un interruptor documentado que convierta Codex en un LLM
conversacional neutro y elimine para siempre todas sus capacidades internas.
El aislamiento por proceso/contenedor sigue siendo obligatorio; los flags no
son una frontera suficiente por sí solos.

## Paso a producción

La migración prevista no cambia el bucle de negocio:

```dotenv
ENVIRONMENT=production
ASSISTANT_RUNTIME=openai_responses
OPENAI_API_KEY=<clave de proyecto>
OPENAI_RESPONSES_MODEL=gpt-5.6
```

Después hay que repetir la evaluación funcional completa —tool calling,
confirmaciones, Brave, streaming, timeouts, voz y respuestas vacías— porque un
agente Codex no se comporta exactamente igual que Responses. También deben
cerrarse contrato, región, retención y observabilidad antes de datos reales.

## Contras conocidas

- Depende de la cuota, capacidad y disponibilidad de la suscripción; no ofrece
  SLA para los usuarios de esta aplicación.
- Todos los usuarios locales compartirían la identidad ChatGPT configurada.
- `dynamicTools` es experimental y puede romperse al actualizar el CLI.
- Codex es un agente orientado a desarrollo, no un runtime conversacional
  municipal neutro.
- Añade OAuth interactivo, binario local, procesos hijos, latencia, TTL y
  complejidad de concurrencia/limpieza.
- Una configuración insuficientemente aislada podría permitir que capacidades
  internas eludieran Brave, RBAC o auditoría; el bridge falla si las detecta,
  pero el aislamiento externo sigue siendo necesario.
- La prueba no valida costes, límites, latencia ni comportamiento exacto de la
  futura API de producción.

Referencias oficiales: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[autenticación de Codex](https://learn.chatgpt.com/docs/auth#openai-authentication)
y [configuración de Codex](https://developers.openai.com/codex/config-reference).
