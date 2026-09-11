# Documentación

Todo lo de esta carpeta está en español. La portada del repositorio es
[`../README.md`](../README.md); la referencia operativa exhaustiva, en inglés,
es [`../README.en.md`](../README.en.md).

## Por dónde empezar

**Si vienes a entender el producto:** [Visión de producto](vision-producto.md) →
[Requisitos](requisitos.md).

**Si vienes a tocar el código:** [Arquitectura](arquitectura.md) →
[Decisiones](decisiones.md) → [CONTRIBUTING](../CONTRIBUTING.md).

**Si vienes a desplegarlo:** [Despliegue](despliegue.md) →
[Protección de datos](proteccion-datos.md).

## Producto

| Documento | Qué contiene | Estado |
|---|---|---|
| [vision-producto.md](vision-producto.md) | Qué quiere ser iConcejo, sus límites explícitos y el modelo de autonomía por riesgo. Fuente principal de las decisiones de producto objetivo. | Vivo · 2026-07-13 |
| [requisitos.md](requisitos.md) | Capacidades ya implementadas. Los requisitos nuevos entran por el módulo de Necesidades como borradores, no editando este fichero. | Vivo · 2026-07-29 |
| [herramientas-asistente.md](herramientas-asistente.md) | Alcance y hoja de ruta de las herramientas de iConcejo: lectura web, adjuntos, análisis, imágenes, conectores y agentes duraderos, con sus puertas de seguridad. | Referencia · 2026-07-16 |

## Arquitectura y decisiones

| Documento | Qué contiene | Estado |
|---|---|---|
| [arquitectura.md](arquitectura.md) | Estado técnico actual: modelo de datos, control de acceso, módulos, colas y almacenamiento. | Vivo · 2026-09-04 |
| [decisiones.md](decisiones.md) | Las 64 ADR del proyecto, numeradas hasta la ADR-065 y con su contexto. Es el fichero más grande de la carpeta y el que hay que leer antes de discutir un diseño. | Vivo |
| [diseno-multiagente.md](diseno-multiagente.md) | Especificación cerrada de la estructura multi-agente del asistente. | Especificación |
| [investigacion-api-ia.md](investigacion-api-ia.md) | Informe que sustentó la elección de LLM y de voz. Material histórico: los precios y modelos han cambiado. | Histórico · 2026-06 |

## Módulos

| Documento | Qué contiene | Estado |
|---|---|---|
| [mapa-municipal.md](mapa-municipal.md) | El plano del municipio: fichero estático de Catastro + OpenStreetMap que viaja con el frontend, sin servidor de mapas ni teselas externas (ADR-064). | Vivo · 2026-09-04 |
| [inventario-municipal.md](inventario-municipal.md) | Bienes e instalaciones municipales. | Vivo · 2026-07-15 |
| [mantenimiento-municipal.md](mantenimiento-municipal.md) | Partes e incidencias sobre esos bienes. | Vivo · 2026-07-15 |
| [biblioteca-ordenanzas.md](biblioteca-ordenanzas.md) | Biblioteca de normativa: importación, revisión humana obligatoria y comparación temática. | Referencia |
| [oficina-agentes.md](oficina-agentes.md) | Oficina de agentes municipales: trabajos supervisados en segundo plano. | Vivo · 2026-06-29 |
| [catalogo-municipal-castilla-leon.md](catalogo-municipal-castilla-leon.md) | Cobertura oficial del catálogo de municipios y la procedencia de cada dato (INE, IGN). | Referencia |
| [diseno-dialogo-voz.md](diseno-dialogo-voz.md) | Especificación del diálogo por voz con iConcejo, ya implementada (ADR-021). | Implementado · 2026-07-08 |
| [diseno-ayuntamiento-prototipo.md](diseno-ayuntamiento-prototipo.md) | Diseño de las pantallas de Ayuntamiento e Instalaciones. | Vivo · 2026-09-04 |

## Operación

| Documento | Qué contiene | Estado |
|---|---|---|
| [despliegue.md](despliegue.md) | Runbook de producción: endurecimiento del servidor, DNS y TLS, secretos, retirada del bootstrap, copias de seguridad y ensayo de restauración. | Vivo · 2026-08-31 |
| [proteccion-datos.md](proteccion-datos.md) | Postura de protección de datos del piloto y las puertas que siguen abiertas antes de ampliarlo. Léelo antes de meter datos reales. | Vivo · 2026-07-30 |
| [runtime-codex-suscripcion.md](runtime-codex-suscripcion.md) | Puente local con suscripción Codex: sólo para evaluación en desarrollo, prohibido en producción (ADR-024). | Referencia · 2026-07-17 |

## Histórico

| Documento | Qué contiene |
|---|---|
| [ordenanzas-burgos-cobertura-2026-06-27.md](ordenanzas-burgos-cobertura-2026-06-27.md) | Reporte de cobertura del Sprint 1 de ordenanzas de Burgos. Fotografía de una fecha concreta; no describe el estado actual. |

## Convenciones

- Los documentos vivos llevan una cabecera `Actualizado: <fecha>` y se
  actualizan en commits de documentación propios, después del commit de la
  funcionalidad.
- Las decisiones técnicas no se argumentan en un documento suelto: se registran
  como ADR numerada en [decisiones.md](decisiones.md) y se citan desde el
  mensaje de commit ("See ADR-012").
- `requisitos.md` describe sólo el estado actual. Una necesidad nueva se
  registra en el módulo de Necesidades de la aplicación, no aquí.
