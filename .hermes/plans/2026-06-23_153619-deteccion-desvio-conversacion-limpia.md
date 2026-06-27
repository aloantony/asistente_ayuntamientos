# Plan coste-efectivo para detectar cuándo abrir una conversación limpia

**Objetivo:** Detectar cuándo la conversación se ha desviado lo suficiente como para proponer una conversación limpia y ahorrar tokens, pero sin gastar más tokens en la detección de los que se pretende ahorrar.

**Arquitectura:** No usar un clasificador permanente en cada turno. Usar una política por niveles: coste cero en hilos cortos, heurística barata en hilos largos, análisis semántico ligero solo en casos dudosos de alto impacto, y resumen puente solo cuando realmente se vaya a cambiar de conversación.

**Principio clave:** La detección de ahorro de contexto debe tener coste marginal casi nulo en la mayoría de turnos. Si analizar el reset cuesta demasiado, el mecanismo se contradice.

**Estado git al redactar:** rama `codex/python-suite-runner-20260618`; `.hermes/` aparece como no trackeado.

---

## 1. Cambio de enfoque respecto al plan original

El plan original era correcto como diseño conceptual, pero demasiado caro si se interpreta como algo que debe ejecutarse completo en cada mensaje.

Nueva regla:

```text
No clasificar cada turno.
No generar resumen puente cada turno.
No calcular razones largas cada turno.
No llamar a un clasificador semántico salvo que el ahorro esperado sea claramente superior al coste.
```

El sistema debe funcionar como un interruptor oportunista, no como una capa pesada permanente.

---

## 2. Modelo de coste de uso

Antes de decidir si conviene detectar un posible cambio de conversación, estimar:

```text
expected_saving =
  tokens_contexto_arrastrado
  * probabilidad_de_independencia
  * turnos_esperados_del_nuevo_tema

usage_cost =
  tokens_detector
  + coste_de_posible_pregunta_extra
  + riesgo_de_perder_contexto
  + fricción_de_usuario
```

Regla práctica:

```text
Sugerir conversación limpia solo si:
expected_saving > usage_cost * 3
```

El multiplicador 3 protege contra falsos positivos y contra una mala experiencia de usuario.

---

## 3. Política por niveles

### Nivel 0: no hacer nada

Aplicar por defecto.

Condiciones:

- Hilo corto.
- Contexto todavía barato.
- No hay señal clara de cambio de tema.
- El usuario continúa la tarea activa.

Acción:

- Responder normalmente.
- No mencionar tokens.
- No sugerir conversación limpia.
- No generar resumen.

Coste: cero o casi cero.

### Nivel 1: heurística barata

Activar solo cuando el hilo ya empieza a ser caro o hay señales claras de cambio.

Señales baratas:

- El mensaje contiene expresiones como “otra cosa”, “cambiando de tema”, “nuevo tema”, “empecemos de cero”, “olvida lo anterior”.
- El mensaje introduce un objetivo principal distinto.
- No hay referencias al objetivo activo: “eso”, “lo anterior”, “el plan”, “la rama”, “el flujo”, nombres de archivos, decisiones previas.
- La tarea anterior parece completada, pausada o abandonada.
- Los últimos turnos ya no usan información del contexto antiguo.
- El contexto acumulado es largo.

Decisión simple:

```text
Si el contexto no es largo:
  continuar, aunque el tema cambie.

Si el contexto es largo y 3 de estas 4 son ciertas:
  - nuevo objetivo principal
  - cero referencias claras al contexto anterior
  - tarea anterior completada/pausada
  - el nuevo tema parece independiente
Entonces:
  sugerir conversación limpia.
```

Coste: bajo.

### Nivel 2: análisis semántico ligero

Activar solo si:

- El hilo es largo o caro.
- La heurística barata no decide con claridad.
- Un falso positivo sería molesto, pero un falso negativo desperdiciaría muchos tokens.

Entrada máxima al análisis:

```yaml
thread_summary: resumen breve del hilo, no historial completo
last_turns: últimos 2-3 turnos
new_message: mensaje actual
active_task_status: active | paused | completed | unclear
```

Salida esperada:

```yaml
classification: continue | compact_and_continue | suggest_new_chat
confidence: low | medium | high
reason: una frase corta
```

No pedir:

- razones largas;
- score detallado;
- lista completa de dependencias;
- resumen puente completo.

Coste: medio, reservado para pocos casos.

### Nivel 3: resumen puente

Activar solo cuando:

- el usuario acepta abrir conversación limpia;
- o el usuario pidió explícitamente empezar limpio;
- o el sistema va a sugerir firmemente un nuevo hilo.

Formato máximo:

```markdown
Contexto mínimo a conservar:
- Proyecto/área: ...
- Objetivo nuevo: ...
- Restricciones relevantes: ...
- Decisiones vigentes: ...
- Pendiente si volvemos al hilo anterior: ...
```

Si el nuevo tema no depende de nada anterior:

```text
No hace falta llevar contexto.
```

Coste: bajo/medio, pero ocurre pocas veces.

---

## 4. Estados de decisión

Reducir la salida a cuatro estados operativos:

```text
CONTINUE
COMPACT_AND_CONTINUE
SUGGEST_NEW_CHAT
START_NEW_CHAT_IF_USER_EXPLICITLY_ASKED
```

### CONTINUE

Usar cuando el contexto anterior sigue siendo útil o el hilo es barato.

Respuesta: normal, sin mencionar tokens.

### COMPACT_AND_CONTINUE

Usar cuando el tema sigue relacionado, pero el historial completo ya no hace falta.

Respuesta posible:

> Seguimos aquí, pero reduzco el contexto operativo a: [...].

No abrir conversación nueva.

### SUGGEST_NEW_CHAT

Usar cuando:

- el hilo es largo;
- el nuevo objetivo parece independiente;
- no hay tarea activa que dependa del contexto;
- el ahorro esperado compensa la fricción.

Respuesta posible:

> Esto parece independiente del contexto anterior. Para no arrastrar ruido, convendría abrirlo como conversación limpia. No hace falta llevar contexto, salvo: [...].

### START_NEW_CHAT_IF_USER_EXPLICITLY_ASKED

Usar cuando el usuario dice claramente:

- “empecemos de cero”;
- “abre conversación limpia”;
- “olvida lo anterior”;
- “nuevo tema limpio”.

Aun así, si hay riesgo de perder una tarea activa, conservar un resumen mínimo del pendiente.

---

## 5. Qué estado mínimo mantener siempre

Mantener solo un estado muy pequeño y barato:

```yaml
conversation_state:
  active_goal: string | null
  active_project: string | null
  task_status: active | paused | completed | unclear
  last_topic: string | null
  context_size_bucket: short | medium | long | near_limit
```

No mantener por defecto:

- lista larga de decisiones;
- dependencias detalladas;
- razones de clasificación;
- resumen puente actualizado constantemente;
- score completo por turno.

Esos elementos se calculan solo cuando se activa Nivel 2 o Nivel 3.

---

## 6. Umbrales prácticos

Los umbrales exactos dependen del sistema, pero la política debe comportarse así:

### Hilo corto

Aunque haya cambio de tema, normalmente continuar.

Motivo: el ahorro potencial es bajo y preguntar añade fricción.

### Hilo medio

Aplicar heurística barata solo si hay señal explícita de cambio.

Motivo: puede empezar a compensar, pero no conviene interrumpir.

### Hilo largo

Aplicar heurística barata con más frecuencia.

Motivo: el coste de arrastrar contexto ya puede ser relevante.

### Hilo cerca del límite

Sugerir compactar o abrir limpio ante cambios de tema moderados.

Motivo: el riesgo ya no es solo coste, también degradación o pérdida de contexto útil.

---

## 7. Experiencia conversacional coste-efectiva

### No mencionar tokens salvo que ayude

Evitar repetir:

> Para ahorrar tokens...

Preferir:

> Esto parece independiente del contexto anterior; convendría tratarlo como tema limpio.

### No preguntar demasiado

Preguntar tiene coste de tokens y de atención.

Solo preguntar cuando la decisión cambie realmente la experiencia.

### Sugerencia no bloqueante cuando sea posible

Si el usuario hace una pregunta que se puede responder sin reset inmediato:

> Esto parece tema nuevo; puedo responder aquí, pero si vamos a seguir con ello convendría abrirlo limpio.

### Confirmación obligatoria cuando haya tarea activa

Si hay trabajo pendiente:

> Antes de aparcar lo anterior: ¿lo tratamos como paréntesis o cerramos ese hilo?

---

## 8. Casos de decisión

### Caso A: hilo corto + tema nuevo

Acción: CONTINUE.

No compensa abrir limpio.

### Caso B: hilo largo + “cambiando totalmente de tema”

Acción: SUGGEST_NEW_CHAT o START_NEW_CHAT_IF_USER_EXPLICITLY_ASKED.

El coste de detección es mínimo y el ahorro potencial alto.

### Caso C: hilo largo + subtema dentro del mismo proyecto

Acción: COMPACT_AND_CONTINUE.

No abrir limpio; conservar continuidad de decisiones.

### Caso D: hilo muy largo + pregunta genérica no relacionada

Acción: SUGGEST_NEW_CHAT.

Probablemente no necesita contexto anterior.

### Caso E: hilo largo + tarea activa sin cerrar

Acción: CONTINUE o preguntar si es paréntesis.

No resetear automáticamente.

---

## 9. Algoritmo inicial recomendado

```python
def decide_context_transition(state, new_message):
    if state.context_size_bucket in {"short", "medium"}:
        if explicit_clean_start(new_message):
            return "START_NEW_CHAT_IF_USER_EXPLICITLY_ASKED"
        return "CONTINUE"

    cheap_signals = 0

    if looks_like_new_goal(new_message):
        cheap_signals += 1
    if not references_current_context(new_message, state):
        cheap_signals += 1
    if state.task_status in {"paused", "completed"}:
        cheap_signals += 1
    if looks_independent_from_active_project(new_message, state):
        cheap_signals += 1

    if explicit_clean_start(new_message):
        return "START_NEW_CHAT_IF_USER_EXPLICITLY_ASKED"

    if state.task_status == "active" and cheap_signals < 4:
        return "CONTINUE"

    if cheap_signals >= 3:
        return "SUGGEST_NEW_CHAT"

    if state.context_size_bucket == "near_limit" and cheap_signals >= 2:
        return "COMPACT_AND_CONTINUE"

    return "CONTINUE"
```

Esta función debe ser barata y no llamar a un LLM separado por defecto.

---

## 10. Validación orientada al coste de uso

Medir:

- Cuántas veces se ejecuta Nivel 1, Nivel 2 y Nivel 3.
- Tokens gastados en detección.
- Tokens ahorrados por conversaciones limpias.
- Número de preguntas extra generadas por el sistema.
- Tasa de aceptación de sugerencias de nuevo hilo.
- Casos donde el usuario tuvo que repetir contexto perdido.

Métrica principal:

```text
net_token_saving = tokens_ahorrados - tokens_gastados_en_detección_y_confirmaciones
```

Pero no basta con tokens. También medir:

```text
user_friction = sugerencias_rechazadas + preguntas_innecesarias + repeticiones_de_contexto
```

Objetivo:

```text
net_token_saving alto
user_friction bajo
falsos_positivos muy bajos
```

---

## 11. Riesgos y mitigaciones

### Riesgo: gastar tokens para ahorrar tokens

Mitigación:

- Nivel 0 por defecto.
- Nivel 1 barato antes de cualquier análisis semántico.
- Nivel 2 solo si el hilo es caro y hay duda real.

### Riesgo: molestar con sugerencias frecuentes

Mitigación:

- No sugerir en hilos cortos.
- No sugerir si el usuario está claramente continuando.
- Si el usuario rechaza una sugerencia, reducir sensibilidad durante un tramo.

### Riesgo: perder continuidad

Mitigación:

- No resetear con tarea activa salvo petición explícita.
- Usar `COMPACT_AND_CONTINUE` como opción intermedia.
- Generar resumen puente solo al cambiar.

### Riesgo: falsos negativos

Mitigación:

- Aceptable al principio: seguir en el mismo hilo cuesta tokens, pero no rompe la experiencia.
- Ajustar sensibilidad solo cuando el contexto esté cerca del límite.

---

## 12. Primera versión que sí aplicaría

Implementar únicamente esto:

```text
1. Mantener estado mínimo:
   - active_goal
   - active_project
   - task_status
   - context_size_bucket

2. No hacer nada en hilos cortos o medios salvo petición explícita.

3. En hilos largos, usar heurística barata:
   - nuevo objetivo
   - no referencia contexto actual
   - tarea previa cerrada/pausada
   - independencia aparente

4. Si 3 de 4 señales son ciertas:
   - sugerir conversación limpia.

5. Si el tema sigue relacionado pero el hilo pesa:
   - compactar y continuar.

6. Generar resumen puente solo si el usuario acepta o pide explícitamente reset.
```

Esto captura la mayor parte del ahorro potencial con muy poco coste recurrente.

---

## 13. Veredicto coste-efectividad

```text
Plan original completo ejecutado cada turno:
  Coste de uso: alto
  Efectividad neta: media/baja
  Recomendación: no usar así

Plan por niveles:
  Coste de uso: bajo
  Efectividad neta: alta
  Recomendación: usar

Heurística barata en hilos largos:
  Coste de uso: muy bajo
  Efectividad: alta
  Recomendación: primera versión

Clasificador semántico frecuente:
  Coste de uso: medio/alto
  Efectividad marginal: incierta
  Recomendación: reservar para casos dudosos

Resumen puente bajo demanda:
  Coste de uso: bajo/medio
  Efectividad: alta
  Recomendación: usar solo al cambiar
```

Conclusión:

```text
El mecanismo solo merece la pena si casi nunca se activa.
Debe ser una optimización oportunista para hilos largos, no una deliberación obligatoria por turno.
```
