# Debug Invalid JSON Tool Call en chat

> **Para Hermes:** usar `systematic-debugging` antes de tocar código. No aplicar fixes hasta reproducir el fallo y aislar si viene de emisión del modelo, normalización gateway, recuperación textual, bucle de herramientas o render del frontend.

**Objetivo:** encontrar y corregir la causa raíz por la que una intención de guardar una idea/necesidad terminó mostrándose al usuario como JSON técnico y/o con el error `invalid json in tool call - missing function name`.

**Contexto observado:**
- Flujo reproducido por el usuario:
  1. Usuario: `quiero que podamos guardar y gestionar desde un mapa información`
  2. Asistente pregunta tipo de información.
  3. Usuario: `cualquier tipo`
  4. Asistente pregunta organización y problema.
  5. Usuario: `en default. el problema actual es que todo es muy difícil y accesible para nuevos usuarios`
  6. Aparece error técnico en chat: `invalid json in tool call - missing function name`.
- En el transcript después aparece un JSON crudo con forma:
  `{"tool":"save_product_idea","arguments":{...}}`
- El backend actual usa herramientas como `create_requirement`, no `save_product_idea`.
- El gateway ya normaliza algunas formas OpenAI/Hermes: `tool_calls`, `<tool_call>{"name":...}</tool_call>` y JSON standalone con `name`/`tool_name`/`function.name`.
- El caso problemático probable usa clave `tool` en lugar de `name`, o una llamada textual sin envoltorio nativo.

**Git / estado de trabajo:**
- Repo: `/home/dev/proyectos/asistente_ayuntamientos`
- Rama actual al crear este plan: `codex/python-suite-runner-20260618`
- Hay cambios sin commitear previos en:
  - `backend/app/assistant/agents.py`
  - `backend/app/assistant/service.py`
  - `backend/app/assistant/tools.py`
  - `backend/tests/test_assistant.py`
  - varios ficheros frontend
  - `.hermes/` sin trackear
- Antes de implementar, volver a ejecutar `git status --short --branch` y no pisar cambios ajenos.

---

## Fase 1: investigación de causa raíz

### Tarea 1: Reproducir el bug a nivel API

**Objetivo:** tener una reproducción automatizable del flujo real, no solo del síntoma visual.

**Archivos:**
- Leer/modificar test: `backend/tests/test_assistant.py`
- Código bajo inspección: `backend/app/assistant/service.py`
- Código bajo inspección: `backend/app/assistant/gateway.py`

**Pasos:**
1. Añadir un test temporal o definitivo que simule el flujo de mensajes exacto con `FakeGateway`.
2. La respuesta fake debe imitar el runtime que falló. Probar, como mínimo, estas variantes:
   - JSON standalone con `{"tool":"save_product_idea","arguments":{...}}`
   - JSON standalone con `{"tool":"create_requirement","arguments":{...}}`
   - Texto con JSON técnico precedido o seguido de explicación.
3. Ejecutar solo el test nuevo:
   `cd backend && pytest tests/test_assistant.py::<nombre_del_test> -v --tb=long`
4. Guardar evidencia:
   - `assistant_message.content`
   - `assistant_message.actions`
   - `assistant_message.agent_key`
   - `assistant_message.routing`
   - `conversation.state`
   - número y contenido resumido de `gateway.calls`.

**Criterio de éxito:** el test reproduce uno de estos fallos:
- el JSON técnico llega al `content` visible;
- no se ejecuta ninguna acción aunque la respuesta pretendía llamar una herramienta;
- se registra una acción fallida con herramienta inexistente;
- el gateway/runtime produce el error antes de que el backend pueda normalizarlo.

### Tarea 2: Confirmar si el error viene del frontend o del backend

**Objetivo:** saber si el mensaje técnico fue persistido por backend o renderizado por frontend a partir de metadata.

**Archivos:**
- Backend API: `backend/app/assistant/routes.py` si hace falta leer respuesta serializada.
- Frontend render: `frontend/app/components/AssistantPanel.tsx`
- Tipos frontend: `frontend/app/components/types.ts`
- API frontend: `frontend/app/lib/api.ts`

**Pasos:**
1. Inspeccionar la respuesta JSON real del endpoint `/assistant/conversations/{id}/messages` en el test.
2. Verificar si el error aparece en `messages[-1].content`, en `messages[-1].actions`, o en ambos.
3. Leer el render de `AssistantPanel.tsx` para confirmar que no muestra directamente payloads técnicos de `actions` como burbuja principal.
4. Si el backend no persiste el JSON como `content`, crear un test frontend o inspección manual para localizar el render equivocado.

**Criterio de éxito:** componente responsable identificado: backend persistence, gateway normalization, tool loop, o UI rendering.

### Tarea 3: Auditar la normalización de llamadas de herramienta

**Objetivo:** entender qué formatos acepta `gateway._from_openai_response` y cuál falta.

**Archivos:**
- `backend/app/assistant/gateway.py`
- `backend/tests/test_assistant.py`

**Puntos concretos a revisar:**
- `_from_openai_response()` líneas aproximadas 420-459.
- `_parse_standalone_tool_call()` líneas aproximadas 475-491.
- `_parse_tool_call_payload()` líneas aproximadas 506-528.
- `_extract_inline_tool_calls()` líneas aproximadas 462-472.

**Hipótesis inicial:** el normalizador no acepta `{"tool":"...","arguments":...}` porque solo busca `name`, `tool_name` o `function.name`. Si el runtime emitió `tool`, el backend lo dejó como texto visible o el runtime lo rechazó con “missing function name”.

**Criterio de éxito:** confirmar con test unitario si `{"tool":"create_requirement","arguments":{...}}` hoy queda como texto o se ignora.

### Tarea 4: Auditar el contrato de herramientas disponibles

**Objetivo:** explicar por qué apareció `save_product_idea` si el backend parece exponer `create_requirement`.

**Archivos:**
- `backend/app/assistant/agents.py`
- `backend/app/assistant/tools.py`
- `backend/app/assistant/service.py`

**Pasos:**
1. Buscar si `save_product_idea` existe en el repo:
   `search_files("save_product_idea", path="/home/dev/proyectos/asistente_ayuntamientos")`
2. Si no existe, comprobar el prompt enviado al modelo en el test (`gateway.calls[-1]["tools"]`) y confirmar qué nombres de herramienta recibió.
3. Verificar si el modelo inventó una herramienta por semántica o si el frontend/otro runtime esperaba ese nombre.
4. Revisar instrucciones en `COMMON_SYSTEM_PROMPT`: actualmente mencionan el formato `<tool_call>{"name":...}</tool_call>`, pero no contemplan `tool`.

**Criterio de éxito:** responder con evidencia a: ¿el modelo inventó `save_product_idea`, el runtime lo transformó, o había una especificación antigua/desalineada?

---

## Fase 2: análisis de patrones existentes

### Tarea 5: Comparar con tests ya existentes de normalización

**Objetivo:** extender el patrón existente, no crear un arreglo aislado.

**Referencias actuales:**
- `test_hermes_agent_openai_tool_calls_are_normalized`
- `test_hermes_agent_inline_tool_calls_are_normalized`
- `test_hermes_agent_standalone_json_tool_call_is_normalized`
- `test_agent_recovers_hermes_argument_only_read_tool_call`

**Acción:** leer estos tests completos y añadir casos al lado, manteniendo el mismo estilo.

**Criterio de éxito:** nuevo test cubre exactamente el formato que rompió el chat.

### Tarea 6: Comparar recuperación textual read-only vs mutating

**Objetivo:** evitar una solución insegura que ejecute mutaciones desde JSON ambiguo.

**Archivos:**
- `backend/app/assistant/service.py`

**Contexto:**
- `recover_textual_read_tool_call()` solo recupera herramientas read-only a partir de argumentos sin nombre.
- Mutaciones deben requerir un nombre de herramienta explícito y permisos.

**Decisión esperada:**
- Aceptar `tool` como alias de `name` si el payload incluye nombre explícito.
- No inferir herramientas mutating desde argumentos sin nombre.

---

## Fase 3: hipótesis y tests mínimos

### Tarea 7: Crear test unitario para alias `tool`

**Objetivo:** demostrar el bug en el normalizador.

**Archivo:**
- `backend/tests/test_assistant.py`

**Test sugerido:**
```python
def test_hermes_agent_standalone_tool_alias_is_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "tool": "create_requirement",
                                "arguments": {
                                    "organization_id": 1,
                                    "title": "Gestión desde mapa",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }
    )

    assert completion.stop_reason == "tool_use"
    assert len(completion.content) == 1
    assert completion.content[0].name == "create_requirement"
    assert completion.content[0].input == {
        "organization_id": 1,
        "title": "Gestión desde mapa",
    }
```

**Comando:**
`cd backend && pytest tests/test_assistant.py::test_hermes_agent_standalone_tool_alias_is_normalized -v --tb=long`

**Resultado esperado antes del fix:** FAIL, porque no se parsea `tool` como nombre.

### Tarea 8: Crear test de integración para que JSON técnico no sea visible

**Objetivo:** asegurar que el usuario no vuelve a ver llamadas técnicas como respuesta final.

**Archivo:**
- `backend/tests/test_assistant.py`

**Escenario sugerido:**
1. Crear conversación.
2. FakeGateway devuelve primero un payload de herramienta en formato problemático.
3. Después de ejecutar la herramienta, FakeGateway devuelve una respuesta final normal.
4. Assert:
   - `assistant_message.content` no contiene `{`, `"tool"`, `"arguments"`, `<tool_call>` ni `invalid json`.
   - `assistant_message.actions` contiene la herramienta ejecutada o error controlado.
   - Si la herramienta no existe (`save_product_idea`), el usuario recibe mensaje claro, no JSON crudo.

**Nota de seguridad:** si se usa `create_requirement`, dar permisos adecuados al usuario fixture y organización.

---

## Fase 4: implementación según causa confirmada

### Tarea 9: Fix si la causa es alias `tool` no soportado

**Archivo:**
- `backend/app/assistant/gateway.py`

**Cambio mínimo esperado:**
- En `_parse_standalone_tool_call()`, aceptar `parsed.get("tool")` como nombre explícito.
- En `_parse_tool_call_payload()`, aceptar `parsed.get("tool")` como alias de `name`.

**Código orientativo:**
```python
# En _parse_standalone_tool_call
if not (
    parsed.get("name")
    or parsed.get("tool")
    or parsed.get("tool_name")
    or (parsed.get("function") or {}).get("name")
):
    return None

# En _parse_tool_call_payload
name = (
    parsed.get("name")
    or parsed.get("tool")
    or parsed.get("tool_name")
    or function.get("name")
)
```

**Verificación:**
`cd backend && pytest tests/test_assistant.py::test_hermes_agent_standalone_tool_alias_is_normalized -v`

### Tarea 10: Fix si la causa es herramienta inventada/desalineada

**Archivos posibles:**
- `backend/app/assistant/agents.py`
- `backend/app/assistant/service.py`
- `backend/app/assistant/tools.py`

**Regla:** no añadir `save_product_idea` a ciegas si el dominio real es requisitos/necesidades. Primero decidir si “idea de producto” debe mapearse a:
- `create_requirement` como borrador municipal, o
- `propose_transversal_feature` si es funcionalidad reutilizable, o
- una nueva entidad/herramienta si producto quiere separar “ideas” de “necesidades”.

**Preferencia arquitectónica:** representar explícitamente el trabajo pendiente en `conversation.state` (`pending_work`) y ejecutar confirmaciones desde estado estructurado, no desde heurísticas de texto.

### Tarea 11: Fix si la causa es salida textual visible tras fallo de herramienta

**Archivo probable:**
- `backend/app/assistant/service.py`

**Puntos a revisar:**
- Bucle `run_agent_turn()` líneas aproximadas 2006-2071.
- Si una herramienta no existe o falla, se añade a `actions` pero luego se pide respuesta final al modelo.
- Verificar que el contenido final no sea el payload técnico inicial.

**Cambio posible:**
- Si `response.stop_reason == "tool_use"` pero ningún bloque ejecutable válido produce resultado, devolver mensaje de error controlado y loggear, nunca mostrar payload crudo.
- Mantener `actions` auditables.

---

## Validación final

### Backend

Ejecutar pruebas específicas:
```bash
cd /home/dev/proyectos/asistente_ayuntamientos/backend
pytest tests/test_assistant.py::test_hermes_agent_openai_tool_calls_are_normalized -v
pytest tests/test_assistant.py::test_hermes_agent_inline_tool_calls_are_normalized -v
pytest tests/test_assistant.py::test_hermes_agent_standalone_json_tool_call_is_normalized -v
pytest tests/test_assistant.py::<nuevo_test_alias_tool> -v
pytest tests/test_assistant.py::<nuevo_test_no_json_visible> -v
```

Después ejecutar suite de asistente:
```bash
cd /home/dev/proyectos/asistente_ayuntamientos/backend
pytest tests/test_assistant.py -q
```

Si el entorno requiere PostgreSQL/Docker, usar el flujo existente del repo y documentar el comando exacto que pase.

### Frontend

Si se toca render:
```bash
cd /home/dev/proyectos/asistente_ayuntamientos/frontend
npm run lint
npm run build
```

### Verificación manual/API

Repetir el flujo real:
1. `quiero que podamos guardar y gestionar desde un mapa información`
2. `cualquier tipo`
3. `en default. el problema actual es que todo es muy difícil y accesible para nuevos usuarios`

Comprobar:
- no aparece JSON técnico;
- si se guarda algo, aparece acción auditada;
- si falta confirmación, el asistente pregunta de forma natural;
- si hay fallo de herramienta, se muestra un mensaje humano y no payload interno.

---

## Riesgos y decisiones abiertas

- `save_product_idea` puede indicar una intención de producto distinta a `create_requirement`; no conviene simplemente aliasarlo sin decidir el modelo de dominio.
- Aceptar alias `tool` mejora compatibilidad, pero no debe abrir inferencia de mutaciones sin nombre explícito.
- Hay cambios sin commitear existentes; cualquier implementación debe aislar cambios propios y revisar diff antes de guardar.
- El flujo “mapa con cualquier tipo de información” probablemente merece una necesidad/feature real aparte; este plan solo depura el fallo de tool-call/render.

## Definición de terminado

- Existe test rojo-verde que reproduce el formato que causó el error.
- El backend normaliza o rechaza controladamente la llamada problemática.
- El usuario nunca ve JSON de herramientas ni errores técnicos como burbuja principal.
- `actions` conserva auditoría de herramientas ejecutadas/fallidas.
- Tests específicos y `tests/test_assistant.py -q` pasan.
