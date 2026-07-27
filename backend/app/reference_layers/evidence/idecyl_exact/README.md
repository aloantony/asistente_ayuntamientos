# Evidencia exacta IDECyL

`records-20260727.xml` contiene únicamente los 30 registros ISO 19139 que
respaldan las 31 capas `same_exact_source` de la auditoría de 2026-07-27 (dos
capas de zonas húmedas comparten FID). Los bloques `gmd:MD_Metadata` se
extrajeron del snapshot CSW oficial de 197 registros conservando su contenido
XML y normalizando únicamente los finales de línea CRLF a LF y el whitespace
final no semántico:

- SHA-256 del snapshot de origen:
  `ed7b6ea8256313aa1977e6cad89bd0694d752bf519a55705d5c562ecf5180a56`.
- SHA-256 de la auditoría de selección:
  `86f9d379d0c775473bf7bea53dfa59f9b400b73474b6822b0d5ec6d469b4e275`.
- SHA-256 del bundle curado:
  `9e7eb47a169eb22303267378cd762357abe0c97137267e37e6e80c4f720e07d5`.

`manifest-v1.json` vincula cada identidad exacta de capa SIUR con su FID,
metadato, WMS de catálogo, WFS seleccionado, distribución oficial observada,
términos y atribuciones. El cargador valida los bytes y esas relaciones antes
de añadir la evidencia a la definición de fuente.

Estos archivos no conceden autorización por sí mismos. Descargar o promover
un espejo sigue requiriendo una revisión humana persistida y ligada al nuevo
hash de definición.
