# Ajustar explicación de capacidades del asistente municipal

> **Para Hermes:** planificar solo. No implementar en este paso. Si se ejecuta después, usar rama/worktree aislado y aplicar TDD.

**Objetivo:** Evitar que el asistente se presente como “solo consulta” cuando sí puede ayudar operativamente a preparar, registrar o actualizar trabajo revisable, manteniendo límites claros sobre aprobación administrativa, validación legal y revisión humana.

**Arquitectura:** Separar semánticamente “acciones operativas supervisadas” de “decisiones finales restringidas”. El asistente debe explicar capacidades desde el producto assistant-first: convierte conversación en trabajo estructurado, usa herramientas con permisos y deja trazabilidad; no sustituye a responsables humanos.

**Tech stack / zona afectada:** Backend Python/FastAPI, servicio del asistente en `backend/app/assistant/`, tests en `backend/tests/test_assistant.py`. Posible copy visible en frontend solo si hay textos estáticos, pero el caso observado parece originarse en prompts/respuestas del backend/LLM.

---

## Contexto actual

Estado git observado antes de planificar:

```text
Branch: codex/python-suite-runner-20260618
Status:
 M backend/app/assistant/service.py
 M backend/tests/test_assistant.py
 M frontend/app/components/AssistantPanel.tsx
?? .hermes/
Remote: origin git@github.com:aloantony/asistente_ayuntamientos.git
```

Hay cambios preexistentes en backend y frontend. No se debe implementar directamente encima sin confirmar si pertenecen a la tarea actual. Para ejecutar este plan, crear una rama/worktree aislado o revisar primero esos cambios.

Archivos inspeccionados:

- `backend/app/assistant/service.py`
  - `COMMON_SYSTEM_PROMPT` ya dice que el asistente “ayuda a consultar, capturar, estructurar y proponer; las revisiones y aprobaciones las hacen personas”.
  - `build_tool_prompt_block()` etiqueta herramientas como “solo lectura” o “puede modificar datos”. Esto puede inducir al modelo a formular límites técnicos de forma visible.
- `backend/app/assistant/agents.py`
  - `REQUIREMENTS_INTAKE_INSTRUCTIONS` permite crear borradores, actualizar, proponer memoria y registrar aceptación de funcionalidades transversales.
  - `CONSULTATION_INSTRUCTIONS` dice “No crees, actualices ni registres cambios”. Correcto para ese agente, pero peligroso si el usuario pregunta “qué puedes hacer” y el router cae en consulta.
- `backend/app/assistant/planner.py`
  - El router manda a `consultation` cuando el usuario solo pide leer/consultar/resumir/buscar.
  - Si hay duda, elige `requirements_intake`.
- `backend/tests/test_assistant.py`
  - Ya existen tests alrededor de preguntas como “puedes crear un requisito desde aquí?” y de evitar lenguaje interno tipo “solo lectura”/“agente”.

## Problema de producto

La respuesta observada:

```text
Puedo ayudarte a consultar información registrada...
No puedo crear, modificar ni aprobar nada desde aquí; solo consultar...
```

es incorrecta para la experiencia deseada porque:

1. Presenta el producto como chatbot informativo, no como asistente operativo.
2. Mezcla acciones permitidas con acciones restringidas.
3. Obliga al asistente a corregirse en el siguiente turno.
4. Reduce confianza: el usuario entiende que la interfaz no sirve para avanzar trabajo.
5. Filtra una limitación interna del agente activo (“consulta”) como si fuera una incapacidad global del sistema.

## Decisión de producto propuesta

El asistente debe responder a “Qué puedes hacer?” desde una perspectiva global de producto, no desde el subconjunto accidental de herramientas del agente actual.

Copy objetivo:

```text
Puedo ayudarte a convertir una conversación en trabajo estructurado.

Por ejemplo:
- identificar una necesidad municipal;
- hacerte preguntas para concretarla;
- preparar o actualizar un borrador revisable;
- consultar necesidades, proyectos u organizaciones visibles;
- añadir notas o cambios cuando me los confirmes;
- proponer funcionalidades transversales relacionadas;
- dejar una propuesta lista para revisión humana.

Las aprobaciones administrativas, validaciones legales y decisiones finales siguen correspondiendo a una persona responsable.
```

Para el seguimiento “¿a qué te refieres con que no puedes crear, modificar ni aprobar nada?” la respuesta debe ser:

```text
Me expliqué mal.

Sí puedo ayudarte a preparar y actualizar borradores, añadir notas o registrar decisiones operativas cuando tengas permiso y me des una confirmación clara.

Lo que no hago es aprobar oficialmente decisiones administrativas, validar legalmente contenidos ni sustituir la revisión de una persona responsable.
```

## Principios de diseño

1. Primero capacidades, después límites.
2. No usar “solo lectura”, “modo consulta”, “agente”, “herramienta no disponible” ni routing interno en copy visible.
3. Distinguir tres categorías:
   - Permitido: consultar, capturar, estructurar, crear borradores, actualizar necesidades, añadir notas, proponer memoria, sugerir/aceptar funcionalidades transversales con confirmación y permisos.
   - Condicionado: cambios que requieren permisos, organización clara, datos mínimos y confirmación explícita.
   - Restringido: aprobación oficial, validación legal, publicación definitiva, compromisos administrativos, sustitución de revisión humana.
4. El backend determinista debe preservar seguridad, pero la conversación debe seguir siendo natural.
5. Si el agente activo no tiene una herramienta mutante, no responder “no puedo”; debe enrutar/derivar internamente cuando la intención sea operativa.

## Plan de implementación

### Task 1: Crear tests de regresión para “Qué puedes hacer?”

**Objetivo:** Asegurar que una pregunta general de capacidades no produce una respuesta de solo consulta.

**Archivos:**
- Modificar: `backend/tests/test_assistant.py`

**Pasos:**
1. Añadir un test con usuario con permisos `assistant.use`, `requirements.view`, `requirements.create`, y organización accesible.
2. Crear conversación nueva.
3. Enviar `Qué puedes hacer?`.
4. Configurar `FakeGateway` para devolver una respuesta adecuada o, idealmente, probar que el prompt obliga a esa respuesta si el test actual permite inspeccionar llamadas.
5. Verificar que la respuesta:
   - contiene “borrador” o “necesidad”;
   - contiene “revisión humana” o “persona responsable”;
   - no contiene “No puedo crear, modificar”;
   - no contiene “solo consultar”;
   - no contiene “solo lectura”, “agente” ni “modo consulta”.

**Comando esperado:**

```bash
cd /home/dev/proyectos/asistente_ayuntamientos/backend
pytest tests/test_assistant.py -q -k "capacidad or puedes_hacer"
```

Expected inicial: FAIL antes de ajustar prompt/routing si se reproduce la respuesta defectuosa.

### Task 2: Añadir test de corrección ante follow-up de límite mal entendido

**Objetivo:** Asegurar que ante “¿a qué te refieres con que no puedes crear, modificar ni aprobar?” el asistente corrige la distinción sin negar capacidades operativas.

**Archivos:**
- Modificar: `backend/tests/test_assistant.py`

**Pasos:**
1. Simular conversación previa con respuesta problemática o con mención a límites.
2. Enviar el follow-up exacto o similar.
3. Verificar que la respuesta:
   - contiene “me expliqué mal” o equivalente;
   - afirma que puede preparar/actualizar borradores o necesidades;
   - restringe aprobación/validación legal/revisión final;
   - no mantiene “no puedo crear, modificar nada”.

**Nota:** Este test puede necesitar gateway fake si no existe respuesta determinista. Si la respuesta depende del LLM, el test debe inspeccionar el prompt y usar fake response controlada; no hacer assertions frágiles sobre generación libre salvo copy determinista.

### Task 3: Introducir una sección común de “capacidades y límites” en el prompt

**Objetivo:** Dar al modelo un marco claro y reutilizable para explicar capacidades sin mezclar operaciones permitidas con decisiones restringidas.

**Archivos:**
- Modificar: `backend/app/assistant/service.py`

**Cambio conceptual:** En `COMMON_SYSTEM_PROMPT`, sustituir/expandir la línea actual:

```text
No tomas decisiones legales ni administrativas. Ayudas a consultar, capturar, estructurar y proponer; las revisiones y aprobaciones las hacen personas.
```

por una regla más explícita:

```text
Capacidades y límites:
- Presenta primero lo que sí puedes hacer: consultar información visible, descubrir necesidades, preparar o actualizar borradores, añadir notas, proponer memorias revisables y sugerir funcionalidades transversales cuando el usuario tenga permisos y confirme lo necesario.
- No digas de forma general que “no puedes crear, modificar ni aprobar nada”. Es impreciso: sí puedes ayudar a crear o modificar borradores y registros operativos permitidos por herramientas y permisos.
- Distingue siempre entre trabajo operativo supervisado y decisiones finales: no apruebas oficialmente decisiones administrativas, no validas legalmente contenidos y no sustituyes la revisión humana responsable.
- Si el usuario pregunta “qué puedes hacer”, responde desde las capacidades globales del asistente municipal, no desde detalles internos del agente activo.
```

**Riesgo:** Si se añade demasiado texto al prompt, aumenta coste y ruido. Mantenerlo compacto.

### Task 4: Ajustar instrucciones del agente de consulta para no convertir su límite local en incapacidad global

**Objetivo:** Evitar que `consultation` diga que el producto completo “solo consulta”.

**Archivos:**
- Modificar: `backend/app/assistant/agents.py`

**Cambio conceptual:** En `CONSULTATION_INSTRUCTIONS`, mantener el límite de no mutar desde consulta, pero añadir:

```text
- Si el usuario pregunta por las capacidades generales del asistente, explica también que puede ayudar a preparar y actualizar borradores o necesidades cuando la conversación lo requiera, con permisos y confirmación. No presentes el límite de este agente como una incapacidad global del asistente.
- Si el usuario expresa intención de crear, modificar, completar o registrar algo, no respondas que no puedes por estar consultando; deja que el sistema enrute a captura de necesidades o pide el dato mínimo que falte sin mencionar agentes internos.
```

**Alternativa más limpia:** No resolverlo solo por prompt; añadir routing determinista para preguntas generales de capacidades hacia `requirements_intake` o hacia una respuesta determinista común. Ver Task 5.

### Task 5: Evaluar routing determinista para preguntas de capacidades

**Objetivo:** Garantizar comportamiento consistente sin depender del LLM cuando el usuario pregunta “Qué puedes hacer?”.

**Archivos probables:**
- Modificar: `backend/app/assistant/service.py`
- Posiblemente `backend/app/assistant/planner.py`
- Modificar: `backend/tests/test_assistant.py`

**Opción recomendada:** Añadir una detección estrecha para preguntas generales de capacidades:

Ejemplos:
- `qué puedes hacer`
- `que puedes hacer`
- `en qué me puedes ayudar`
- `qué puedes hacer desde aquí`
- `qué acciones puedes hacer`

Respuesta determinista común: usar el copy objetivo anterior.

**Por qué:**
- Es una pregunta de onboarding/capacidades, no una consulta de datos.
- Debe ser estable y producto-controlada.
- Evita que el agente de consulta se autolimite.

**Precaución:** No convertir esto en heurística de flujo de negocio. Es solo una FAQ/capability response de onboarding, no un router complejo.

### Task 6: Ajustar etiquetas internas de herramientas si contaminan el copy visible

**Objetivo:** Reducir la probabilidad de que el modelo repita “solo lectura” al usuario.

**Archivos:**
- Modificar: `backend/app/assistant/service.py`

**Cambio a evaluar:** En `build_tool_prompt_block()`, cambiar:

```python
mode = "solo lectura" if tool.read_only else "puede modificar datos"
```

por etiquetas más internas y menos copiables, por ejemplo:

```text
modo interno: lectura
modo interno: escritura supervisada
```

o mantener las etiquetas pero reforzar en prompt:

```text
Las etiquetas de herramienta son internas; no las menciones al usuario.
```

**Recomendación:** Preferir prompt explícito primero. Cambiar la etiqueta puede afectar tests existentes o el uso del modelo; hacerlo solo si los tests muestran contaminación persistente.

### Task 7: Tests de permisos y límites reales

**Objetivo:** No pasarse al extremo contrario: el asistente no debe prometer crear si el usuario no tiene permisos.

**Archivos:**
- Modificar: `backend/tests/test_assistant.py`

**Casos:**
1. Usuario con solo `assistant.use`/lectura pregunta “qué puedes hacer?”
   - Respuesta debe decir que puede ayudar a preparar información y consultar lo visible.
   - Si habla de crear/actualizar, debe condicionar a permisos y confirmación.
2. Usuario pide “aprueba esta necesidad”
   - Respuesta debe rechazar aprobación oficial y ofrecer preparar/registrar propuesta para revisión.
3. Usuario pide “crea una necesidad” con permisos pero sin contenido suficiente
   - Ya existen tests parecidos; asegurar que siguen pasando.

### Task 8: Ejecutar verificación backend

**Objetivo:** Validar que el cambio no rompe routing, creación directa ni consulta.

**Comandos recomendados:**

```bash
cd /home/dev/proyectos/asistente_ayuntamientos/backend
pytest tests/test_assistant.py -q -k "capacidad or puedes_hacer or direct_create or consultation"
```

Si el entorno host no tiene dependencias, usar fallback Docker del workflow del proyecto:

```bash
cd /home/dev/proyectos/asistente_ayuntamientos/backend
docker run --rm --network host \
  -v "$PWD:/app" \
  -w /app \
  -e PYTHONPATH=/app \
  python:3.12-slim \
  sh -c 'pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt >/tmp/pip.log && pytest tests/test_assistant.py -q -k "capacidad or puedes_hacer or direct_create or consultation"'
```

Después, si pasa el target, ejecutar suite más amplia relevante:

```bash
pytest tests/test_assistant.py -q
```

### Task 9: Validación manual en la app local

**Objetivo:** Confirmar la experiencia real del chat.

**Pasos:**
1. Si se ejecuta en checkout que alimenta Docker Compose, reconstruir backend si procede:

```bash
docker compose build backend worker
docker compose up -d backend worker
```

2. Abrir `/asistente` y probar:
   - `Qué puedes hacer?`
   - `a qué te refieres con que no puedes crear, modificar ni aprobar nada?`
   - `crea una necesidad para ...`
   - `aprueba esta necesidad`

3. Verificar que:
   - no aparece “solo consultar” como incapacidad global;
   - no aparece “agente” ni “modo consulta”;
   - sí aparecen límites humanos/legales;
   - las acciones mutantes siguen pidiendo datos/confirmación cuando toca.

## Archivos probablemente afectados

- `backend/app/assistant/service.py`
  - prompt común;
  - posible respuesta determinista para capacidades;
  - posible detección estrecha de preguntas de capacidades;
  - posible ajuste de etiquetas internas de herramientas.
- `backend/app/assistant/agents.py`
  - instrucciones de consulta e intake.
- `backend/tests/test_assistant.py`
  - regresiones de copy, routing y límites.

No tocar inicialmente:

- modelos de datos;
- permisos/RBAC;
- frontend, salvo que exista copy estático relacionado;
- herramientas de creación/actualización, salvo que tests revelen incoherencia real.

## Riesgos y tradeoffs

1. Riesgo: prometer demasiado.
   - Mitigación: condicionar acciones a permisos, datos mínimos y confirmación.

2. Riesgo: crear heurísticas rígidas de lenguaje.
   - Mitigación: solo usar detección determinista para FAQ de capacidades; mantener creación/actualización en el flujo estructurado existente.

3. Riesgo: tests frágiles por copy exacto.
   - Mitigación: assertions por ideas prohibidas/obligatorias, no por párrafo completo salvo respuesta determinista.

4. Riesgo: ocultar límites legales.
   - Mitigación: todo copy de capacidades debe cerrar con aprobación/validación/revisión humana.

5. Riesgo: cambios preexistentes en la rama actual.
   - Mitigación: ejecutar en worktree/rama aislada y stagear solo archivos de esta tarea.

## Criterios de aceptación

- Ante `Qué puedes hacer?`, el asistente explica capacidades operativas y límites humanos sin decir “solo consultar”.
- Ante preguntas sobre creación/modificación, distingue borradores/registros permitidos de aprobaciones oficiales.
- No se menciona routing, agentes internos, modo consulta ni solo lectura al usuario.
- Se mantienen permisos y confirmaciones para acciones mutantes.
- Tests backend relevantes pasan con salida real.
- Handoff final incluye branch, archivos cambiados, tests ejecutados y estado git.

## Recomendación de ejecución

1. Crear worktree aislado desde la base acordada, porque la rama actual tiene cambios preexistentes.
2. Implementar primero tests de regresión.
3. Ajustar prompt común e instrucciones.
4. Añadir respuesta determinista para “Qué puedes hacer?” si el test demuestra variabilidad o si se quiere copy estable de producto.
5. Ejecutar tests focalizados.
6. Validar manualmente en app local si el usuario quiere probar la UX.
