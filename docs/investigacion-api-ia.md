# Informe de investigación — Selección de LLM y voz (STT) para "Asistente Ayuntamientos"

**Fecha del informe:** 2026-06-12. **Moneda:** todos los precios en USD (ningún proveedor publica tarifas en EUR salvo el conmutador EUR de Mistral, no capturado; existe exposición a tipo de cambio). **Alcance:** carga de trabajo actual — agente conversacional de toma de requisitos en español, síncrono, 7 herramientas JSON, prompt de sistema ~1,5K tokens, contextos <20K tokens, 5–30 turnos por conversación, 1–4 llamadas LLM por turno, latencia interactiva (pocos segundos), comprador: administración pública española (RGPD). Las cargas futuras (comparación de ordenanzas, redacción de documentos) se mencionan solo como criterio de "camino de subida", no se optimiza para ellas.

---

## 1. Resumen ejecutivo

**Recomendación principal (HOY): Mistral Small 4 en La Plateforme (API de Mistral, procesamiento en la UE por defecto), condicionada a una evaluación previa.**

Razones:
- **Encaje RGPD insuperable para este comprador:** Mistral es empresa de la UE (sede en París), procesa por defecto en centros de datos de la UE, ofrece DPA a todos los clientes de negocio y no tiene exposición al CLOUD Act estadounidense — el argumento de soberanía más limpio frente a un ayuntamiento español ([mistral.ai/pricing](https://mistral.ai/pricing), [help.mistral.ai — almacenamiento de datos](https://help.mistral.ai/en/articles/347629-where-do-you-store-my-data-or-my-organization-s-data), consultados 2026-06-12).
- **Precio en el suelo del mercado:** $0,10 / $0,30 por 1M tokens (entrada/salida). Incluso en el escenario alto (50.000 turnos/mes) el coste estimado es ~$75/mes.
- **Capacidad suficiente para el listón de calidad:** function calling con esquema JSON, llamadas paralelas y `tool_choice`; cobertura fuerte de español por su enfoque europeo de entrenamiento.
- **Condición:** la fiabilidad de tool use de Mistral está menos probada que la de Anthropic/OpenAI y su documentación de function calling va por detrás de la página de precios (cita Small 3.2 en vez de Small 4). **Antes de comprometerse, ejecutar una evaluación de ~50 conversaciones reales en español con el esquema real de las 7 herramientas.** Si la tasa de fallo de herramientas no es aceptable, pasar a la segunda opción.

**Segunda opción: Claude Haiku 4.5 vía Amazon Bedrock con endpoints regionales UE** (Fráncfort/París/Irlanda/España, prima del 10% sobre precio global). Es la opción con la fiabilidad de tool use más sólida del estudio (soporta `strict: true` — entradas de herramienta garantizadas válidas contra el esquema — y caché de prompts con lectura a 0,1×), con residencia de datos en la UE donde AWS es el encargado del tratamiento y el personal de Anthropic no tiene acceso a la infraestructura ([docs Anthropic — Bedrock](https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock), [pricing](https://platform.claude.com/docs/en/about-claude/pricing), 2026-06-12). Coste estimado: ~$935/mes en el escenario alto sin caché; ~$500–600 con caché. Ventaja adicional de cara a licitación: la infraestructura AWS suele contar con certificación ENS (verificar nivel exacto — no confirmado en esta investigación).

**Palancas de coste a activar HOY:**
1. **Mantener el prefijo del prompt estable** (sistema + definiciones de herramientas primero, contenido volátil al final) para aprovechar caché donde exista: en Anthropic la lectura de caché cuesta 0,1× y la escritura 1,25× (TTL 5 min); en OpenAI el descuento de entrada cacheada es del 90% y automático. Ojo: en Haiku 4.5 el prefijo cacheable mínimo es ~4.096 tokens (~2.048 en Sonnet 4.6) — el prompt de sistema de 1,5K tokens solo no cachea; cacheará cuando se acumule historial. Verificar con `usage.cache_read_input_tokens`.
2. **No usar Batch para el chat** (es asíncrono), pero anotarlo para el futuro: descuento plano del 50% en Anthropic, OpenAI, Gemini y Mistral — ideal para la comparación de ordenanzas.
3. **Capa de abstracción de modelo + snapshots fijados:** el catálogo de modelos pequeños rota cada 6–12 meses (OpenAI retira varios GPT-5.x en jul–ago 2026; DeepSeek renombra modelos en jul 2026).
4. **En modelos con control de razonamiento** (gpt-5.4-mini `reasoning_effort`, Sonnet 4.6 `effort`), fijarlo en bajo para esta carga interactiva.

---

## 2. Verificación cruzada — cifras marcadas

Cifras que conviene re-verificar antes de presupuestar (el resto de precios fue contrastado contra páginas oficiales el 2026-06-12; los de Anthropic se contrastaron además contra la referencia interna de la API de Claude con los mismos valores):

| Bandera | Detalle |
|---|---|
| **Mistral Medium 3.5 vs Large 3** | La página oficial lista Medium 3.5 a $1,50/$7,50 pero Large 3 a $0,50/$1,50 (¡el "Large" más barato que el "Medium"!) y agregadores aún citan Medium 3 a $0,40/$2,00. Inconsistente — re-verificar en mistral.ai antes de usar como ruta de subida. |
| **gpt-5-mini legado ($0,25/$2,00)** | Precio de OpenRouter, no de OpenAI (ya no aparece en su página oficial). Tratar como indicativo y probable candidato a retirada. |
| **Gemini 2.5 Flash-Lite ($0,10/$0,40)** | El más barato de Google, pero **sin disponibilidad confirmada en región UE de Vertex** — su ventaja de precio puede ser inutilizable para este comprador. |
| **Sonnet 4.6 en Bedrock UE** | No aparece en la oferta nueva "Claude in Amazon Bedrock" (Messages API); solo vía integración legada. Confirmar con AWS si se necesita esa combinación. |
| **whisper-1 ($0,006/min)** | Ya no figura en la página oficial de precios de OpenAI; corroborado solo por fuentes secundarias — posible vía de retirada. |
| **Azure (~$1/h) y Google STT (~$0,016/min)** | Páginas oficiales no verificables directamente (timeout/tabla truncada); cifras de fuentes secundarias 2025–2026. |
| **Tarifas streaming de Deepgram** | Marcadas como "promocionales por tiempo limitado" en su propia página — presupuestar con la tarifa pre-grabada ($0,0092/min). |
| **Subidas del 10% el 2026-07-01** | Dos a la vez: endpoints regionales (no globales) de Gemini 3+ en Vertex, y procesamiento in-region UE de AssemblyAI. Cualquier modelo de costes UE debe usar ya los precios con prima. |
| **Residencia UE de Groq** | Centro de datos en Helsinki confirmado (jul 2025), pero **no** se pudo confirmar si el plan self-serve permite fijar región UE — solo válido tras confirmación contractual. |
| **ENS (Esquema Nacional de Seguridad)** | Probablemente determinante en contratación pública española y **no investigado a fondo**: Azure y Google Cloud lo tienen; AWS presumiblemente también (verificar); OpenAI, Anthropic (API directa), Mistral, AssemblyAI y Deepgram casi seguro no lo publicitan. Puede pesar más que el RGPD puro. |

---

## 3. Tabla comparativa de precios (USD por 1M tokens)

Todos los precios consultados el **2026-06-12** en páginas oficiales salvo indicación. "UE" = precio en el endpoint con residencia europea cuando difiere.

| Proveedor / Modelo | Entrada | Salida | Caché (lectura) | Nota UE | Fuente |
|---|---|---|---|---|---|
| **Mistral Small 4** | $0,10 | $0,30 | no documentada | UE por defecto, sin prima | [mistral.ai/pricing](https://mistral.ai/pricing) |
| Mistral Ministral 14B | $0,20 | $0,20 | no documentada | UE por defecto | [mistral.ai/pricing](https://mistral.ai/pricing) |
| Mistral Medium 3.5 | $1,50 | $7,50 (⚠ ver banderas) | no documentada | UE por defecto | [mistral.ai/pricing](https://mistral.ai/pricing) |
| **Gemini 3.1 Flash-Lite** | $0,25 | $1,50 | $0,025 (explícita) + $1/M tok/h almacenamiento; implícita gratis sin garantía | Vertex UE: $0,275/$1,65 desde 2026-07-01 | [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing) (act. 2026-06-09), [Vertex pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing) |
| Gemini 2.5 Flash-Lite | $0,10 | $0,40 | $0,01 | ⚠ UE no confirmada | [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Gemini 3.5 Flash | $1,50 | $9,00 | $0,15 | Vertex UE: $1,65/$9,90 | [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| **OpenAI gpt-5.4-mini** | $0,75 | $4,50 | $0,075 (auto, −90%) | UE: $0,825/$4,95 (+10%, requiere aprobación) | [developers.openai.com/api/docs/pricing](https://developers.openai.com/api/docs/pricing) |
| OpenAI gpt-5.4-nano | $0,20 | $1,25 | $0,02 | +10% UE; ⚠ tool use es su punto débil | [developers.openai.com/api/docs/models/gpt-5.4-nano](https://developers.openai.com/api/docs/models/gpt-5.4-nano) |
| OpenAI gpt-5.4 | $2,50 | $15,00 | $0,25 | +10% UE | [developers.openai.com/api/docs/pricing](https://developers.openai.com/api/docs/pricing) |
| **Claude Haiku 4.5** | $1,00 | $5,00 | $0,10 lectura; $1,25 escritura 5 min | Bedrock UE: +10% (~$1,10/$5,50) | [platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Claude Sonnet 4.6 | $3,00 | $15,00 | $0,30 lectura; $3,75 escritura | API directa sin opción UE; Bedrock vía integración legada | [platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Groq gpt-oss-120B | $0,15 | $0,60 | — | DC Helsinki; ⚠ fijación de región sin confirmar | [groq.com/pricing](https://groq.com/pricing) |
| Groq Llama 3.3 70B | $0,59 | $0,79 | — | ídem | [groq.com/pricing](https://groq.com/pricing) |
| DeepSeek V4 Flash | $0,14 | $0,28 | $0,0028 | **Descartado** (datos en China; sanción del Garante italiano; veto checo en AAPP) | [api-docs.deepseek.com/quick_start/pricing](https://api-docs.deepseek.com/quick_start/pricing) |

Descuento Batch (asíncrono, irrelevante para el chat, útil para ordenanzas futuras): 50% plano en Anthropic, OpenAI, Gemini y Mistral, según las mismas fuentes.

---

## 4. Coste mensual estimado — 3 escenarios

**Supuestos:** 2 llamadas por turno; por llamada ~6.000 tokens de entrada y ~500 de salida.

- Por turno: 2 × 6.000 = **12.000 tokens entrada**; 2 × 500 = **1.000 tokens salida**.
- 500 turnos/mes → 6M entrada, 0,5M salida.
- 5.000 turnos/mes → 60M entrada, 5M salida.
- 50.000 turnos/mes → 600M entrada, 50M salida.

Fórmula: `coste = (M_entrada × precio_entrada) + (M_salida × precio_salida)`. Ejemplo (Haiku 4.5 Bedrock UE, 50K turnos): `600 × $1,10 + 50 × $5,50 = $660 + $275 = $935`.

**Sin caché (precios con residencia UE donde aplica):**

| Modelo (endpoint UE) | 500 turnos/mes | 5.000 turnos/mes | 50.000 turnos/mes |
|---|---|---|---|
| Mistral Small 4 | 6×0,10 + 0,5×0,30 = **$0,75** | **$7,50** | 600×0,10 + 50×0,30 = **$75** |
| Gemini 3.1 Flash-Lite (Vertex UE) | 6×0,275 + 0,5×1,65 = **$2,48** | **$24,75** | 600×0,275 + 50×1,65 = **$247,50** |
| gpt-5.4-mini (UE) | 6×0,825 + 0,5×4,95 = **$7,43** | **$74,25** | 600×0,825 + 50×4,95 = **$742,50** |
| Claude Haiku 4.5 (Bedrock UE) | 6×1,10 + 0,5×5,50 = **$9,35** | **$93,50** | **$935** |
| Claude Sonnet 4.6 (referencia calidad, precio global) | 6×3 + 0,5×15 = **$25,50** | **$255** | **$2.550** |

**Efecto de la caché de prompts (estimación, no garantía):** en una conversación multi-turno, gran parte de los 6K de entrada por llamada es prefijo repetido (sistema + 7 herramientas + historial). Suponiendo ~70% de la entrada servida desde caché:
- **Haiku 4.5:** multiplicador efectivo de entrada ≈ 0,3×1,25 + 0,7×0,1 = 0,445 → escenario alto ≈ 600×1,10×0,445 + 275 ≈ **~$570/mes** (−39%). Condición: el prefijo debe superar ~4.096 tokens (se cumple a partir de los primeros turnos, no en el turno 1).
- **gpt-5.4-mini:** entrada cacheada al 10% del precio, automática → escenario alto ≈ 600×(0,3×0,825 + 0,7×0,0825) + 247,50 ≈ **~$430/mes** (−42%).
- **Gemini Flash-Lite:** la caché implícita es gratuita pero "sin garantía de ahorro" y con mínimos de 2.048–4.096 tokens; presupuestar a precio completo como caso conservador.
- **Mistral:** sin caché documentada; el precio base ya es tan bajo que no cambia la conclusión.

**Conclusión de coste:** incluso en el peor escenario razonable (50K turnos, Sonnet 4.6 sin caché) el LLM cuesta <$3K/mes, y con las opciones recomendadas queda entre **$75 y ~$950/mes**. El coste no debe ser el criterio decisivo; la fiabilidad de herramientas y el encaje RGPD/ENS sí.

---

## 5. Cumplimiento RGPD/UE por proveedor (crítico para AAPP española)

| Proveedor | Residencia UE | DPA / transferencias | Retención | Exposición CLOUD Act | Veredicto |
|---|---|---|---|---|---|
| **Mistral (La Plateforme)** | Sí, por defecto (región París); endpoint EEUU es opt-in | DPA incluido para clientes de negocio; SCC donde haya subencargados no UE | 30 días rotatorios para supervisión de abusos, luego borrado ([help.mistral.ai](https://help.mistral.ai/en/articles/347629-where-do-you-store-my-data-or-my-organization-s-data)) | **No** (empresa UE de extremo a extremo) | **El más limpio.** Pendiente: ENS no verificado. |
| **Anthropic — API directa** | **No** (`inference_geo` solo `global`/`us`; datos en reposo solo EEUU) ([docs data-residency](https://platform.claude.com/docs/en/manage-claude/data-residency)) | DPA auto-incorporado (vigente 2025-02-24), SCC Módulos 2+3 (Irlanda), anexos UK/Suiza, preaviso de 15 días para subencargados ([anthropic.com/legal/data-processing-addendum](https://www.anthropic.com/legal/data-processing-addendum)) | Sin retención de contenido por defecto; ZDR bajo petición (cubre Messages API y caché) ([docs retención](https://platform.claude.com/docs/en/build-with-claude/api-and-data-retention)) | Sí | Defendible vía DPA+SCC, pero sin residencia UE: argumento débil ante un ayuntamiento. |
| **Anthropic vía Amazon Bedrock UE** | Sí: Fráncfort, París, Irlanda, **España**, Estocolmo, Zúrich, Milán; prima 10% ([docs Bedrock](https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock)) | AWS es el encargado; DPA de AWS; cero acceso del personal de Anthropic a la infraestructura | Según términos AWS | Sí (AWS es empresa de EEUU), mitigado por residencia UE | **La mejor vía "Claude" para este comprador.** AWS además suele tener ENS (verificar). |
| **OpenAI (eu.api.openai.com)** | Sí: la UE soporta almacenamiento en reposo **y** procesamiento regional; **requiere aprobación** (monitoreo de abuso modificado/ZDR); +10% en modelos post-2026-03-05 ([guía Your Data](https://developers.openai.com/api/docs/guides/your-data)) | DPA actualizado 2026-01-01; OpenAI Ireland Ltd + SCC; SOC 2 Type 2, CSA STAR ([openai.com/policies/data-processing-addendum](https://openai.com/policies/data-processing-addendum/)) | API no entrena por defecto; logs de abuso ≤30 días; ZDR para aprobados | Sí | Sólido, pero el proceso de aprobación UE para un proveedor pequeño es un paso de riesgo no cuantificado. Sin ENS publicitado. |
| **Google — Gemini Developer API (AI Studio)** | **No**: los términos permiten almacenar/cachear "en cualquier país" ([ai.google.dev/gemini-api/terms](https://ai.google.dev/gemini-api/terms)) | DPA de procesador en nivel de pago; no entrena con datos de pago | Logs de abuso "por periodo limitado" | Sí | **Bloqueante en su forma Developer API.** |
| **Google — Vertex AI (UE)** | Sí: endpoint multi-región UE `aiplatform.eu.rep.googleapis.com` con gemini-3.5-flash y 3.1-flash-lite (⚠ triangulado de foros/snippets, confirmar en docs oficiales); fijación estricta de país solo para 2.5 Pro/2.0 Flash; +10% desde 2026-07-01 | Google Cloud DPA; términos tipo ZDR negociables con ventas | Logs de abuso por defecto | Sí | Viable; GCP tiene ENS — punto fuerte en licitación. |
| **Groq / Together / Fireworks** | Groq: DC Helsinki (jul 2025) pero fijación self-serve sin confirmar; Fireworks: UE solo en despliegues dedicados; Together: construcción UE hasta 2028 | DPAs publicados con SCC (Groq) | Variable | Sí, los tres | Solo Groq merece seguimiento; ninguno listo hoy sin verificación contractual. |
| **DeepSeek (API propia)** | No — datos almacenados en China | Garante italiano: limitación definitiva (ene 2025, infracciones Cap. V y art. 32; DeepSeek alegó que el RGPD no le aplica) ([twobirds.com — resolución Garante](https://www.twobirds.com/en/insights/2025/the-garante-imposes-a-definitive-limitation-on-the-processing-of-italian-users%E2%80%99-personal-data)); veto checo en administración pública (jul 2025) | — | — | **Descartado.** (Sus pesos abiertos servidos por un host occidental serían jurídicamente otra cosa.) |

**Nota transversal — ENS:** ningún hallazgo verificó certificaciones ENS en esta sesión salvo referencias a Azure y Google Cloud. Antes de cualquier licitación, confirmar el requisito ENS con el ayuntamiento y la certificación del proveedor elegido; puede invertir el orden de la recomendación (favoreciendo rutas vía hyperscaler: Bedrock o Vertex).

---

## 6. Voz (STT) para v1

El coste de STT es despreciable frente al LLM (10.000 mensajes de 1 minuto ≈ $25–60/mes en las APIs baratas, $0 en navegador o auto-alojado). La decisión debe basarse en privacidad, cobertura de navegador y calidad de español.

**Recomendación v1 — Web Speech API del navegador, SOLO en modo local (`processLocally = true`):**
- Gratis y con español (`es-ES`). **Matiz de privacidad crítico:** en su modo por defecto, Chrome envía el audio a los servidores de Google **sin DPA que cubra ese tratamiento** — inaceptable para datos de una administración pública ([MDN Web Speech API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API), [caniuse](https://caniuse.com/speech-recognition)).
- Chrome 139+ (ago 2025) añade reconocimiento en el dispositivo: con `processLocally = true` ni el audio ni la transcripción salen del equipo ([MDN processLocally](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition/processLocally), [chromestatus](https://chromestatus.com/feature/6090916291674112)). Implementación segura: comprobar en runtime `SpeechRecognition.available({processLocally:true, langs:['es-ES']})`; si no está disponible (Safari, Firefox, packs de idioma ausentes), **no activar el modo cloud por defecto del navegador** — degradar a "sin botón de voz" o usar el fallback de pago.
- Limitaciones honestas: solo Chromium; disponibilidad del pack de español no confirmada en esta investigación (probar en dispositivo real); Firefox no soporta Web Speech en la práctica.

**Alternativa de pago / fallback (elegir una):**
1. **Whisper auto-alojado (faster-whisper / whisper.cpp)** — la posición RGPD más fuerte posible: el audio nunca sale de tu infraestructura, sin cadena de encargados. Licencia MIT, $0/min marginal; large-v3 int8 cabe en ~2,5 GB VRAM (una GPU de 8 GB tipo RTX 3060, ~300 EUR, va sobrada para clips de 15–60 s; CPU sola sirve con modelos small/medium). Español es idioma de primer nivel en Whisper (~3–6% WER en audio limpio; large-v3 4,38% WER en Common Voice ES) ([faster-whisper](https://github.com/SYSTRAN/faster-whisper), [whisper.cpp](https://github.com/ggml-org/whisper.cpp)). Coste real: operación + un servidor, no tarifas por minuto. Recomendado si ya vais a operar infraestructura propia.
2. **AssemblyAI con endpoint UE** (`api.eu.assemblyai.com`) — $0,15/hora de audio (~$0,0025/min), español incluido, residencia UE al mismo precio hoy (**+10% in-region desde 2026-07-01** → ~$0,165/h) ([assemblyai.com/pricing](https://www.assemblyai.com/pricing), 2026-06-12). Para clips de chat, usar subida de fichero asíncrona (la facturación streaming cuenta el tiempo de socket abierto, incluido el inactivo). Alternativas comparables: OpenAI `gpt-4o-mini-transcribe` ~$0,003/min con residencia UE ([pricing](https://developers.openai.com/api/docs/pricing), [residencia UE](https://openai.com/index/introducing-data-residency-in-europe/)), Deepgram Nova-3 con endpoint UE ($0,0092/min pre-grabado). Caveat común: ninguno de estos publicita ENS; Azure Speech (~$0,0167/min) y Google STT (~$0,016/min) son 3–6× más caros pero sus nubes sí tienen ENS — relevante solo si el requisito ENS alcanza también al STT.

---

## 7. Riesgos y cuándo reevaluar

**Riesgos principales:**
1. **Fiabilidad de tool use de la opción primaria sin validar.** El listón es exactamente ese. Mitigación: la evaluación de ~50 conversaciones con las 7 herramientas reales es bloqueante antes de elegir; si Mistral Small 4 falla, Haiku 4.5 (Bedrock UE) con `strict: true` es el plan B ya costeado.
2. **Rotación de catálogo.** OpenAI retira snapshots GPT-5.x en jul–ago 2026 ([deprecations](https://developers.openai.com/api/docs/deprecations)); DeepSeek renombra en jul 2026; docs de Mistral van por detrás de su pricing. Mitigación: capa de abstracción de proveedor + snapshots con fecha + smoke test de herramientas en CI.
3. **Cambios de precio ya anunciados para 2026-07-01** (Gemini regional +10%, AssemblyAI UE +10%) y precios en USD con exposición a FX. Cualquier presupuesto en EUR debe llevar margen.
4. **ENS sin verificar** (ver banderas). Puede forzar la ruta hyperscaler (Bedrock/Vertex) con independencia del análisis técnico.
5. **Aprobaciones y procesos comerciales no self-serve:** residencia UE de OpenAI (aprobación), ZDR de Anthropic (vía ventas), términos tipo ZDR de Vertex (vía ventas), región UE de Groq (sin confirmar). No asumir ninguno como inmediato.
6. **Límites de tasa a escala:** Anthropic exige escalar de tier (Tier 1: 50 RPM; planificar Tier 2+ pronto y Tier 4/facturación mensual en despliegue completo — [rate limits](https://platform.claude.com/docs/en/api/rate-limits)); Google ya no publica límites estáticos (mirar en AI Studio por proyecto). Onboardings simultáneos de muchos municipios pueden disparar 429.
7. **Calidad de español: evidencia cualitativa, no benchmark.** No se encontró benchmark de español específico y reciente para los modelos pequeños candidatos (las cifras de MMLU español 0,88–0,91 son de la generación GPT-5 de 2025). La evaluación propia es la única medida fiable.

**Cuándo reevaluar (disparadores concretos):**
- **Antes de codificar:** resultado de la evaluación de 50 conversaciones (tasa de fallo de herramientas, calidad de español, latencia p95).
- **Al superar ~5.000 turnos/mes:** revisar límites de tasa, considerar caché formalmente y renegociar/verificar tiers.
- **Al arrancar la comparación de ordenanzas / redacción** (carga futura): reabrir el análisis — ahí pesan contextos largos, Batch al 50%, y modelos de gama media/alta (Sonnet 4.6, gpt-5.4, Gemini 3.5 Flash, Mistral Medium si se aclara su precio).
- **2026-07-01:** entran en vigor las dos subidas del 10% — recalcular la tabla de escenarios.
- **Si aparece el requisito ENS por escrito en un pliego:** decidir entre ruta Bedrock/Vertex o aclaración formal con el proveedor.
- **Cada ~6 meses en cualquier caso:** los precios y catálogos de esta tabla caducan rápido; este informe refleja el 2026-06-12.