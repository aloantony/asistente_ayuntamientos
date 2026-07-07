# Especificación de implementación — Diálogo por voz con Anacleto

Actualizado: 2026-07-07 · Estado: **cerrado para implementación** · Ejecutor previsto: agente Codex · Aprobación del alcance: Anthony

Este documento es una especificación ejecutable: todas las decisiones están tomadas (§2) y los contratos son literales (§4). El implementador no debe reabrir decisiones; si algo resulta inviable tal cual está escrito, debe pararse y reportarlo, no improvisar una alternativa.

## 0. Instrucciones para el agente implementador

**Prerrequisito:** esta especificación asume el asistente conversacional v2 (ADR-020: motor `turn.py`, streaming SSE `POST /assistant/conversations/{id}/messages/stream`, `prompts.py`) integrado en la rama de trabajo. Si ese trabajo aún no está en `main`, implementar encima de la rama que lo contiene. Si no existe en la rama actual, **parar y reportar**.

Reglas del repositorio que aplican a este trabajo (resumen de `CLAUDE.md`, que debe leerse entero antes de empezar):

1. Nunca editar ni commitear `.env`; `.env.example` es la plantilla documentada.
2. Logs de los servicios de voz **solo con metadatos** (runtime, bytes, duración, idioma, códigos de estado); jamás audio, transcripciones, texto sintetizado ni claves.
3. No introducir dependencias nuevas de pip ni de npm: el cliente HTTP para Azure es `urllib.request` de la stdlib (mismo estilo que `app/telegram/routes.py`). Nada de SDK de Azure.
4. Sin migraciones Alembic: ningún cambio de este alcance persiste columnas nuevas.
5. Tests backend obligatorios (§6) con el arnés existente (`backend/tests/conftest.py`); los tests jamás llaman a Azure/NVIDIA reales.
6. Commits en inglés, sujeto imperativo, **sin prefijos conventional-commit** (`feat:`, `fix:`… prohibidos), cuerpo citando "See ADR-021". Docs en español y en commits de docs separados. Plan de commits en §7.
7. Trabajar en rama; no hacer push ni merge sin OK de Anthony.
8. No tocar `app/assistant/gateway.py` (el egreso LLM no cambia), ni el almacenamiento de documentos, ni nada fuera del alcance listado.

Orden de ejecución: PT-0 → PT-1 → PT-2. **PT-3 queda explícitamente fuera de esta orden** (§5.6). Al cerrar cada paquete, ejecutar su bloque de validación (§8).

## 1. Contexto

Requisito indispensable del alcalde (usuario primario, no técnico, en español): dialogar por voz con Anacleto en la web. Restricción de infraestructura: **sin autoalojado; pipeline de voz 100% nube gestionada.**

Ya existe en el código: grabación con `MediaRecorder` en `AssistantPanel.tsx`, transcripción en servidor `POST /assistant/audio-transcriptions` → `app/assistant/speech.py` (runtime NVIDIA NIM/Whisper, commit 79ecd0a), notas de voz en Telegram, y streaming SSE de respuestas (v2). Falta: síntesis de voz (TTS), auto-envío del transcript, estilo de respuesta oral y regularización de gobernanza (el egreso de audio a NVIDIA no está cubierto por ningún ADR y contradice ADR-012).

## 2. Decisiones cerradas

| # | Decisión | Resolución |
|---|---|---|
| D1 | Proveedor TTS | **Azure AI Speech (REST)**, recurso creado en la suscripción **Azure for Students** de Anthony (solo desarrollo/evaluación; sus términos excluyen producción). Antes de datos reales: mismo código sobre recurso en suscripción pay-as-you-go propia (solo cambia `.env`). |
| D2 | Región y voz | Región UE admitida por la política de la suscripción (las subs de estudiante vetan regiones llenas — `westeurope` rechazada el 2026-07-07 con `RequestDisallowedByAzure`; probar en orden `northeurope`, `swedencentral`, `francecentral`, `germanywestcentral`). `AZURE_SPEECH_REGION` debe coincidir con la región del recurso creado. Voz por defecto **`es-ES-ElviraNeural`**, configurable por entorno; las voces son las mismas en todas las regiones. Sin selector de voz/velocidad en UI. |
| D3 | Formato de audio | `audio-24khz-48kbitrate-mono-mp3` → respuesta `audio/mpeg` (compatible con `<audio>` en Chrome/Edge/Safari/Firefox). |
| D4 | Runtimes TTS | `SPEECH_SYNTHESIS_RUNTIME = disabled \| azure`. Solo esos dos en v1. **Sin fallback a `speechSynthesis` del navegador**: runtime `disabled` ⇒ la web funciona solo con texto y el modo voz no se ofrece. |
| D5 | STT | Se mantiene `nvidia_nim` tal cual (ya construido), legitimado por ADR-021 como proveedor en evaluación. Se documenta y testea; no se migra en esta orden. |
| D6 | Estilo oral | Campo `input_mode: "text"\|"voice"` por mensaje (request-scoped, sin persistencia); con `voice`, el system prompt añade el bloque del Anexo C. Telegram lo envía como `voice` cuando el mensaje llegó como nota de voz. |
| D7 | UX | Todo en el composer del panel actual (sin overlay a pantalla completa). PT-1: un gesto por turno. PT-2: manos libres con parada por silencio y re-escucha; único ajuste nuevo persistido: autoescucha on/off. |
| D8 | Locución | PT-1 sintetiza al evento `done` (una llamada por turno). PT-2 trocea los `text_delta` en frases y sintetiza por frase (cola FIFO). |
| D9 | Gobernanza | ADR-021 (texto cerrado en Anexo A) supersede ADR-012 y reconoce `speech.py` como segundo punto de egreso con la disciplina del gateway. Se actualizan `CLAUDE.md`, `.env.example`, README y `diseno-multiagente.md`. |

## 3. Arquitectura

```
micrófono ──MediaRecorder──▶ POST /assistant/audio-transcriptions ──▶ STT nube (nvidia_nim)
   ▲                                                                        │ texto
   │ re-armar escucha (PT-2)                                                ▼
   │                                POST /conversations/{id}/messages/stream (SSE)
   │                                        │ text_delta · input_mode=voice → estilo oral
   │                                        ▼
altavoz ◀── <audio> ◀── POST /assistant/speech ──▶ TTS nube (azure, es-ES-ElviraNeural)
```

Medio-dúplex estricto: nunca hay micrófono abierto mientras suena la locución; interrumpir es un gesto explícito (tap). El navegador solo captura y reproduce; toda la voz en nube pasa por los dos endpoints propios del backend.

## 4. Contratos

### 4.1 Configuración (`app/core/config.py` + `.env.example`)

Añadir a `Settings` (defaults exactos):

```python
speech_synthesis_runtime: str = "disabled"          # validator: {"disabled", "azure"}
speech_synthesis_voice: str = "es-ES-ElviraNeural"
speech_synthesis_language_code: str = "es-ES"
speech_synthesis_max_chars: int = 3000
speech_synthesis_timeout_seconds: float = 30.0
azure_speech_key: str | None = None
azure_speech_region: str = "westeurope"
```

Con `field_validator` para `speech_synthesis_runtime` análogo al de `speech_transcription_runtime`. Cambio adicional **ya aplicado en la rama (2026-07-07)**: `nvidia_whisper_function_id` ya no trae function-id hardcodeada (default `None`) y la parte STT del bloque "Voz" ya está en `.env.example` — no rehacer. ⚠️ Migración para cualquier `.env` real con `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`: debe añadir `NVIDIA_WHISPER_FUNCTION_ID` o el STT devolverá 503. El bloque completo (STT + TTS) de `.env.example` está en el Anexo B.

### 4.2 Backend — `app/assistant/speech.py`

Añadir, siguiendo el estilo del transcriber existente:

- `class SpeechSynthesisError(RuntimeError)`.
- `class SpeechSynthesizer(Protocol)` con `synthesize(self, text: str) -> bytes`.
- `class DisabledSpeechSynthesizer` — siempre lanza `SpeechSynthesisError("Speech synthesis is disabled")`.
- `class AzureSpeechSynthesizer`:
  - Si falta `settings.azure_speech_key` → `SpeechSynthesisError`.
  - `POST https://{azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1` con `urllib.request` y `timeout=settings.speech_synthesis_timeout_seconds`.
  - Cabeceras: `Ocp-Apim-Subscription-Key: {key}`, `Content-Type: application/ssml+xml`, `X-Microsoft-OutputFormat: audio-24khz-48kbitrate-mono-mp3`, `User-Agent: asistente-ayuntamientos`.
  - Cuerpo: `build_azure_ssml(text, voice, language)` — **función pura a nivel de módulo** (para test unitario) que devuelve `<speak version='1.0' xml:lang='{language}'><voice name='{voice}'>{texto escapado con xml.sax.saxutils.escape}</voice></speak>`.
  - Error HTTP/red (`urllib.error.*`, respuesta vacía) → log `logger.warning("Azure speech synthesis failed", exc_info=True)` y `SpeechSynthesisError("Speech synthesis failed")`. Nunca loguear el texto.
- `def get_speech_synthesizer() -> SpeechSynthesizer` — según `settings.speech_synthesis_runtime`.
- `def synthesize_speech_bytes(text: str) -> bytes` — función de módulo (punto de monkeypatch en tests, igual que `transcribe_audio_bytes`).

### 4.3 Backend — endpoint `POST /assistant/speech` (`routes.py`)

Espejo del endpoint de transcripción (líneas 91-114):

- Body: `AssistantSpeechCreate` (nuevo en `schemas.py`): `text: str = Field(min_length=1, max_length=20000)` con `str_strip_whitespace=True`.
- Auth: `get_current_user` + `require_assistant_use(db, current_user)`.
- `len(payload.text) > settings.speech_synthesis_max_chars` → **413** `"Speech text is too long"`.
- `SpeechSynthesisError` → **503** `"Speech synthesis is not available"`.
- Éxito → `fastapi.Response(content=audio, media_type="audio/mpeg")`.
- Importar `synthesize_speech_bytes` en `routes.py` (mismo patrón de import que la transcripción, para que el monkeypatch de tests sea `app.assistant.routes.synthesize_speech_bytes`).

### 4.4 Backend — `input_mode` y prompt oral

- `AssistantUserMessageCreate` (schemas.py:169) añade `input_mode: Literal["text", "voice"] = "text"`.
- `AssistantStatusRead` (schemas.py:59) añade `speech_transcription_enabled: bool = False` y `speech_synthesis_enabled: bool = False`; en `get_assistant_status` se rellenan con `settings.speech_transcription_runtime != "disabled"` y `settings.speech_synthesis_runtime != "disabled"`.
- `run_agent_turn_events` y `run_agent_turn` (`turn.py`) aceptan `input_mode: str = "text"` (keyword-only) y lo pasan a `build_system_prompt`.
- `build_system_prompt(db, current_user, tools, input_mode="text")` (`prompts.py`): si `input_mode == "voice"`, concatena al final el bloque `VOICE_MODE_PROMPT_BLOCK` (constante de módulo, texto exacto en Anexo C).
- `send_message` y `send_message_stream` (`routes.py`) pasan `input_mode=payload.input_mode`.
- Telegram (`app/telegram/routes.py`): cuando `text` proviene de `transcribe_telegram_voice` (línea ~163), la llamada `run_agent_turn(...)` de la línea ~169 lleva `input_mode="voice"`. La respuesta sigue siendo texto.

### 4.5 Frontend — API (`app/lib/api.ts`)

- `streamAssistantMessage(conversationId, content, accessToken, handlers, inputMode: "text" | "voice" = "text")` — el body pasa a `JSON.stringify({ content, input_mode: inputMode })`.
- Nueva `export async function synthesizeAssistantSpeech(text: string, accessToken: string): Promise<Blob>` — `performAdminRequest` a `/assistant/speech` con `{ method: "POST", body: JSON.stringify({ text }) }` y `response.blob()`. Errores por el camino estándar (`readApiError`).
- `translateApiDetail`: añadir casos `"Audio transcription is not available"` → `"La transcripción de voz no está disponible ahora mismo."`, `"Speech synthesis is not available"` → `"La voz del asistente no está disponible ahora mismo."`, `"Speech text is too long"` → `"La respuesta es demasiado larga para leerla en voz alta."`.
- `types.ts`: `AssistantStatus` añade `speech_transcription_enabled: boolean` y `speech_synthesis_enabled: boolean`.

### 4.6 Frontend — `app/lib/voice.ts` (nuevo módulo)

Sin dependencias nuevas. Exporta:

- `flattenMarkdownForSpeech(markdown: string): string` — orden de transformaciones: (1) bloques ``` ``` y líneas de tabla (`|`) se sustituyen —una sola vez por respuesta— por `"Te dejo el detalle escrito en pantalla."`; (2) enlaces `[texto](url)` → `texto`; (3) quitar `#`, `*`, `_`, `>`, `` ` ``; (4) ítems de lista (`- `, `1. `) → la línea termina en punto; (5) colapsar espacios/saltos repetidos.
- `createSpeechPlayer(deps: { synthesize(text: string): Promise<Blob> })` que devuelve:
  - `speak(text: string): Promise<void>` — encola el texto (PT-1: se llama una vez por turno; PT-2: una vez por frase), sintetiza y reproduce en un `HTMLAudioElement` con `URL.createObjectURL`; libera el object URL al terminar; reproducción secuencial FIFO.
  - `stop(): void` — corta reproducción, vacía la cola y aborta síntesis en vuelo (`AbortController`).
  - `subscribe(listener: (speaking: boolean) => void)` — para el estado "hablando".
- PT-2 añade `createSentenceChunker(onSentence: (s: string) => void)`: acumula deltas; emite cuando el buffer contiene fin de frase (`. ! ? …` seguido de espacio, o salto de línea) y lleva ≥ 60 caracteres; `flush()` al `done`. Constantes ajustables agrupadas al inicio del módulo.
- PT-2 añade el detector de silencio: `createSilenceDetector(stream: MediaStream, opts)` con `AudioContext` + `AnalyserNode` (RMS cada ~100 ms): callback `onSilence` tras **1400 ms** por debajo del umbral una vez detectada voz; umbral = `max(0.01, 3 × ruido de fondo)` calibrado en los primeros 500 ms; `onTimeout` si no hay voz en **15 s**; tope duro de utterance **60 s**.

### 4.7 Frontend — controlador y panel

`useAssistantController.ts`:

- Estado nuevo: `voiceModeEnabled` (persistido en `localStorage` clave `assistant.voice.mode`), `handsFreeEnabled` (PT-2, clave `assistant.voice.handsfree`, default `true`), `isSpeaking`.
- `sendMessage` se generaliza: `sendMessage(options?: { contentOverride?: string; inputMode?: "text" | "voice" })` — mantiene el flujo optimista actual; con `contentOverride` no toca `draftMessage`.
- En turnos de voz: al `onDone`, `flattenMarkdownForSpeech(event.message.content)` → `speechPlayer.speak(...)` (PT-1). PT-2: chunker alimentado desde `onTextDelta` con síntesis por frase, y `flush` en `onDone`.
- `stopSpeaking()` expuesto; cancelar locución al cambiar/deseleccionar conversación, al enviar un mensaje nuevo y al desmontar.

`AssistantPanel.tsx`:

- El botón de micrófono actual se muestra solo si además `assistantStatus.speech_transcription_enabled`.
- Toggle "Modo voz" junto al micro, visible solo si `speech_transcription_enabled && speech_synthesis_enabled && speechSupported`. Textos: activo `"Modo voz activado"`, inactivo `"Modo voz"`, `aria-pressed` correcto.
- Con modo voz activo, `appendTranscribedAudio` no rellena el borrador: si el transcript no está vacío llama a `onSendVoiceTranscript(transcript)` (→ `sendMessage({ contentOverride, inputMode: "voice" })`). Vacío → mismo aviso actual.
- Indicador de estado bajo el composer (reutilizar tokens Bral de `styles.css`, clases nuevas con prefijo `voice-`): `Escuchando…` / `Transcribiendo…` (existentes), `Pensando…` (turno en curso), `Hablando…` + botón `"Detener voz"` (`stopSpeaking`).
- PT-2: máquina de estados del modo manos libres:

```
inactivo ──(toggle on + pulsar micro)──▶ escuchando ──(silencio 1,4 s | stop manual)──▶ transcribiendo
transcribiendo ──(transcript ok)──▶ pensando ──(frases/done)──▶ hablando
hablando ──(cola vacía + autoescucha on)──▶ escuchando        (bucle)
hablando ──(tap micro)──▶ escuchando (corta locución)
cualquiera ──(toggle off | error | timeout 15 s sin voz | pestaña oculta)──▶ inactivo (pausa)
```

`document.visibilitychange` → `hidden` pausa el bucle (corta escucha y locución); un error de red/API corta el bucle y muestra el error estándar del panel.

## 5. Paquetes de trabajo

### PT-0 — Gobernanza y consolidación (código + docs)

1. *(Ya aplicado en la rama, 2026-07-07 — no rehacer)* `config.py`: `nvidia_whisper_function_id` default → `None`; parte STT del bloque "Voz" en `.env.example`.
2. `.env.example`: completar el bloque "Voz" con la parte TTS del Anexo B (la parte STT ya existe; dejar el bloque final idéntico al anexo).
3. Tests del endpoint de transcripción (§6, grupo A).
4. `CLAUDE.md`, regla dura de egreso: sustituir la frase "All external AI calls and private agent runtime calls go through the Privacy/AI Gateway (`backend/app/assistant/gateway.py`) — the single egress point." por "All external AI calls and private agent runtime calls go through the Privacy/AI Gateway (`backend/app/assistant/gateway.py`); the voice pipeline (STT/TTS) egresses only through `backend/app/assistant/speech.py` under the same discipline (ADR-021). No other module may call external AI services."
5. `docs/decisiones.md`: añadir ADR-021 (texto literal, Anexo A).
6. `docs/diseno-multiagente.md` punto 3 ("Dictado por voz: APARCADO…"): sustituir por "3. Diálogo por voz: reactivado como requisito indispensable; ver `docs/diseno-dialogo-voz.md` y ADR-021."
7. README: subsección de configuración "Voz (STT/TTS)" con las variables y el aviso students/producción.

### PT-1 — Diálogo por turnos

Backend: §4.1 (resto), §4.2, §4.3, §4.4 + tests grupos B y C. Frontend: §4.5, §4.6 (`flattenMarkdownForSpeech`, `createSpeechPlayer`), §4.7 sin manos libres (tras hablar, vuelve a `inactivo`).

**Aceptación PT-1:** con `SPEECH_SYNTHESIS_RUNTIME=azure` y credenciales en `.env`: activar modo voz → pulsar micro → hablar → parar → el mensaje se envía solo, la respuesta llega en streaming visible y al completarse se oye en es-ES; "Detener voz" corta el audio; con runtime `disabled` el toggle no aparece y todo lo demás funciona como hoy; suite de tests verde.

### PT-2 — Manos libres

Frontend únicamente: §4.6 (chunker + detector de silencio), §4.7 (máquina de estados, autoescucha, ajuste `handsFreeEnabled`, guardas). Sin cambios backend.

**Aceptación PT-2:** conversación de ≥3 turnos sin tocar el teclado ni el ratón (hablar → pausa → respuesta hablada por frases → re-escucha); la primera frase suena antes de terminar el turno; tap durante la locución corta y escucha; 15 s sin hablar pausa el modo; ocultar la pestaña pausa el bucle; con autoescucha off se comporta como PT-1.

### PT-3 — Fuera de alcance de esta orden (no implementar)

Respuesta con nota de voz en Telegram (`sendVoice`), migración del recurso a pay-as-you-go y unificación de STT en Azure, selector de voz/velocidad, overlay a pantalla completa, barge-in por voz. Requieren orden nueva.

## 6. Tests (pytest, `backend/tests/test_assistant.py`)

Patrones: monkeypatch de `app.assistant.routes.transcribe_audio_bytes` / `app.assistant.routes.synthesize_speech_bytes`; `FakeGateway` + fixture `use_gateway` para el prompt; `assistant_user` y `headers_for` existentes. Ningún test toca red.

Grupo A — transcripción (PT-0):
- `test_transcribe_audio_returns_text` — monkeypatch devuelve `"hola"`; POST multipart → 200 `{"text": "hola"}`.
- `test_transcribe_audio_requires_assistant_use` — usuario sin permiso → 403.
- `test_transcribe_audio_rejects_large_file` — `monkeypatch.setattr(settings, "speech_transcription_max_bytes", 10)` + payload mayor → 413.
- `test_transcribe_audio_unavailable_when_disabled` — monkeypatch lanza `SpeechTranscriptionError` → 503, detail exacto.

Grupo B — síntesis (PT-1):
- `test_speech_synthesis_returns_audio` — monkeypatch devuelve `b"mp3-bytes"` → 200, `content-type: audio/mpeg`, body exacto.
- `test_speech_synthesis_requires_assistant_use` → 403.
- `test_speech_synthesis_rejects_long_text` — `monkeypatch.setattr(settings, "speech_synthesis_max_chars", 5)` → 413.
- `test_speech_synthesis_unavailable_when_disabled` — runtime real `disabled` sin monkeypatch del synthesizer → 503.
- `test_azure_ssml_escapes_markup` — unit de `build_azure_ssml("<hola & adiós>", ...)`: contiene `&lt;hola &amp; adiós&gt;`, `xml:lang` y el nombre de voz.

Grupo C — estilo oral (PT-1):
- `test_voice_input_mode_adds_oral_style_prompt` — mensaje con `input_mode="voice"` → `gateway.calls[0]["system"]` contiene `"escuchará tu respuesta en voz alta"`.
- `test_text_input_mode_keeps_prompt_clean` — sin `input_mode` → el system NO contiene esa frase.
- `test_status_reports_speech_flags` — `/assistant/status` incluye ambos flags coherentes con settings monkeypatcheados.

## 7. Plan de commits

1. `Harden voice transcription config and add audio endpoint tests` (PT-0 código: config + tests; body: "See ADR-021").
2. `Document voice pipeline decisions` (PT-0 docs: ADR-021, CLAUDE.md, .env.example, README, diseno-multiagente.md).
3. `Add speech synthesis endpoint and voice input mode` (PT-1 backend + tests).
4. `Add voice dialogue mode to assistant panel` (PT-1 frontend).
5. `Add hands-free voice conversation loop` (PT-2 frontend).
6. `Sync docs after voice dialogue v1` (estado final: README §voz, `arquitectura.md`, `requisitos.md`, y actualizar la cabecera de este documento a "implementado").

## 8. Validación

Tras PT-0 y PT-1 (backend): suite completa en Docker (comando de `CLAUDE.md`). Tras PT-1 y PT-2 (frontend): `npm --prefix frontend run build`. Al cerrar todo, el paquete `/validar` completo: tests + `python3 -m compileall -q backend/app backend/alembic` + builds de imágenes + `alembic current` + `git diff --check`.

Checklist manual (Anthony, con el `.env` real): la de los criterios de aceptación de PT-1 y PT-2 más: (1) Telegram nota de voz sigue funcionando y responde en estilo breve; (2) error de red a mitad de locución corta el bucle con mensaje; (3) modo oscuro y claro; (4) sesión del alcalde (permisos no-admin) ve el toggle. Audición con el alcalde: si Elvira no convence, cambiar `SPEECH_SYNTHESIS_VOICE` (p. ej. `es-ES-AlvaroNeural`) es solo `.env`.

## Anexo A — ADR-021 (texto para `docs/decisiones.md`)

```markdown
## ADR-021: Pipeline de voz en nube gestionada con doble punto de egreso (2026-07-07)

El diálogo por voz pasa a ser requisito de producto (petición del alcalde) y se implementa 100% sobre nube gestionada: no hay infraestructura propia donde autoalojar Whisper/Piper, así que se supersede ADR-012 (reconocimiento solo local en el navegador). La entrada de voz ya egresaba audio a NVIDIA NIM (`app/assistant/speech.py`, runtime `nvidia_nim`) sin ADR que lo cubriera; esta decisión lo regulariza: `speech.py` queda reconocido como el segundo y último punto de egreso de IA junto a `gateway.py`, con su misma disciplina — por él solo viajan audio del turno y texto de respuesta del asistente, nunca documentos originales, y los logs registran únicamente metadatos.

La síntesis de voz usa Azure AI Speech (REST, voz `es-ES-ElviraNeural`, runtime conmutable `SPEECH_SYNTHESIS_RUNTIME=disabled|azure`) sobre una suscripción Azure for Students, válida solo para desarrollo y evaluación: sus términos excluyen cargas comerciales/de producción, caduca con la condición de estudiante y suspende recursos al agotar el crédito. Antes de operar con datos reales, el recurso Speech debe recrearse en una suscripción pay-as-you-go propia (cambio limitado a variables de entorno) y revisarse la posición DPA/ENS del proveedor; el STT `nvidia_nim` queda como proveedor en evaluación con la misma condición y candidatos de sustitución ya investigados (docs/investigacion-api-ia.md §6.2).

El estilo oral se decide por turno con `input_mode` (web y Telegram) y solo altera el system prompt; no se persisten columnas nuevas. El navegador no usa reconocimiento ni síntesis propios (se retira la vía `processLocally` de ADR-012 y no hay fallback a `speechSynthesis`): captura y reproduce, y toda la voz pasa por los endpoints del backend, manteniendo RBAC, tenancy y auditoría existentes. Especificación completa: docs/diseno-dialogo-voz.md.
```

## Anexo B — Bloque para `.env.example` (añadir tras la sección Telegram)

```bash
# Voz (ADR-021): STT y TTS en nube gestionada; ambos deshabilitados por defecto.
# El audio del usuario y el texto de respuesta salen SOLO por app/assistant/speech.py.
# STT (transcripción). Runtimes: disabled | nvidia_nim
SPEECH_TRANSCRIPTION_RUNTIME=disabled
SPEECH_TRANSCRIPTION_LANGUAGE_CODE=es-ES
SPEECH_TRANSCRIPTION_MAX_BYTES=20971520
NVIDIA_API_KEY=
NVIDIA_RIVA_SERVER=grpc.nvcf.nvidia.com:443
# Function-id del Whisper alojado en NVIDIA API (build.nvidia.com):
NVIDIA_WHISPER_FUNCTION_ID=b702f636-f60c-4a3d-a6f4-f3568c13bd7d
# TTS (síntesis). Runtimes: disabled | azure
# Recurso "Speech" de Azure AI Services. Suscripción Azure for Students = solo
# desarrollo/evaluación; producción requiere suscripción pay-as-you-go propia.
SPEECH_SYNTHESIS_RUNTIME=disabled
SPEECH_SYNTHESIS_VOICE=es-ES-ElviraNeural
SPEECH_SYNTHESIS_LANGUAGE_CODE=es-ES
SPEECH_SYNTHESIS_MAX_CHARS=3000
SPEECH_SYNTHESIS_TIMEOUT_SECONDS=30
AZURE_SPEECH_KEY=
# Región del recurso Speech. Debe ser una región UE admitida por la política de la
# suscripción (p. ej. northeurope, swedencentral, francecentral, germanywestcentral).
AZURE_SPEECH_REGION=northeurope
```

## Anexo C — Bloque de prompt para modo voz (constante `VOICE_MODE_PROMPT_BLOCK` en `prompts.py`)

```
Modo voz:
- El usuario está hablando por voz y escuchará tu respuesta en voz alta.
- Responde en 2 a 4 frases naturales de estilo oral, sin Markdown: nada de listas, tablas, encabezados ni bloques de código.
- Si el resultado es extenso o estructurado, resume lo esencial de palabra y termina indicando que dejas el detalle escrito en pantalla.
```

(Se concatena al final del system prompt con `\n\n` solo cuando `input_mode == "voice"`.)

## Anexo D — Referencias (consultadas 2026-07-07)

- Azure AI Speech: [precios](https://azure.microsoft.com/en-us/pricing/details/speech/) (neural $16/M caracteres, free tier 500k caracteres/mes) · [voces es-ES](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support).
- Azure for Students: [programa](https://learn.microsoft.com/en-us/azure/education-hub/about-azure-for-students) · [oferta MS-AZR-0170P](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p) — educación/evaluación sí; producción/uso comercial no.
- NVIDIA (STT actual): [Whisper NIM](https://build.nvidia.com/) · alternativa TTS descartada por acento: [magpie-tts-multilingual, solo es-US](https://docs.nvidia.com/nim/speech/latest/reference/support-matrix/tts.html).
- OpenAI TTS (alternativa descartada en v1): [precios](https://developers.openai.com/api/docs/pricing) · [gpt-4o-mini-tts](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts).
- STT con residencia UE para la futura migración: `docs/investigacion-api-ia.md` §6.2.
