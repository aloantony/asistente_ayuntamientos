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

`manifest-v1.json` conserva el inventario de identidades y las rutas que la
primera auditoría propuso explorar. Está **sustituido como configuración
efectiva**: sus WFS, directorios HTTP y atribuciones inferidas no se convierten
en fuentes locales.

`decision-manifest-v2.json` registra la auditoría posterior de las 31
identidades contra las capacidades WMS/WFS y el contenido real de las
distribuciones:

- 30 quedan `restricted`: los WFS publican IGCYL-NC y paginación no
  transaccional, el ZIP incluye `Licencia-IGCYL.txt` o, para SIGPAC 2022 y
  2024, existe una distribución HTTPS exacta por las nueve provincias que
  sigue sujeta a IGCYL-NC y a revisión humana;
- solo la capa 39 queda como `candidate`, mediante el ZIP GeoPackage HTTPS de
  datos abiertos. Su archivo, GeoPackage, esquema, recuento, CRS, límites,
  muestra y SLD se fijan por huellas exactas.

Los índices raíz y provinciales de SIGPAC se fijan por SHA-256, junto con el
conjunto ordenado de nueve ZIP. Esta evidencia corrige el falso bloqueo de
transporte, pero no crea una fuente ejecutable ni concede descarga,
conservación o servicio local.

La candidata conserva una advertencia explícita: el registro oficial indica
que los datos de cobertura se adquirieron mediante una suscripción externa a
GeoHash. La auditoría técnica no interpreta ese dato como permiso de tercero.

Ninguno de estos archivos concede autorización por sí mismo. La candidata
puede prepararse técnicamente, pero descargar/promover un espejo sigue
requiriendo una revisión humana persistida y ligada al hash de definición.
