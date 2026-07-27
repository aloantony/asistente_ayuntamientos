# Revisión de autorizaciones del espejo SIUR

Estado de contraste: 2026-07-26.

La evaluación técnica específica de las ortofotos históricas se conserva en
[`siur-ign-pnoa-historico-review.md`](siur-ign-pnoa-historico-review.md).

Este documento conserva la evidencia operativa usada para preparar las
revisiones del espejo. No concede permisos, no sustituye una revisión humana y
no permite aplicar automáticamente ninguna autorización. Las condiciones
particulares de cada conjunto prevalecen sobre las condiciones generales del
proveedor.

Cada autorización persistida debe corresponder a una fuente exacta y conservar:

- el identificador y el hash de definición vigentes de la fuente;
- la decisión y los permisos concretos de descarga, conservación y servicio;
- el texto de licencia, URL, atribución y fecha revisados;
- los hashes exactos del documento y de su representación semántica;
- la identidad y fecha de la persona revisora.

## Matriz por proveedor

| Proveedor | Evidencia oficial | Resultado para preparar revisiones |
| --- | --- | --- |
| Junta de Castilla y León / IDECyL | El [aviso legal de Gobierno Abierto](https://gobiernoabierto.jcyl.es/web/jcyl/GobiernoAbierto/es/Plantilla100Detalle/1284216489702/Texto%20Generico/1246989714819/Texto) permite reutilización comercial y no comercial, incluida copia, difusión, modificación, adaptación, extracción y combinación. Exige citar «Origen de los datos: Junta de Castilla y León», conservar fecha y metadatos, no desnaturalizar ni sugerir patrocinio. Una licencia particular prevalece y ciertos contenidos multimedia de terceros requieren autorización expresa. | Candidata a aprobación solo cuando el conjunto exacto no declare una licencia particular o material de terceros incompatible. |
| MITECO | El [aviso legal de Datos Abiertos MITECO](https://www.datosabiertos.miteco.gob.es/es/aviso-legal.html) permite reutilización comercial y no comercial, incluidos datos en bruto, copia, difusión, transformación y combinación. Exige atribución, fecha de actualización, conservación de metadatos, no desnaturalización y ausencia de patrocinio implícito. | Candidata a aprobación tras comprobar las condiciones particulares del conjunto exacto. |
| IGN / CNIG | La [política de datos del IGN](https://www.ign.es/web/info-aviso-legal) y su [licencia de productos y servicios geográficos](https://www.ign.es/resources/licencia/Condiciones_licenciaUso_IGN.pdf) establecen una licencia compatible con CC BY 4.0. Exigen atribución visible específica del producto; una obra derivada debe indicarlo también en sus metadatos y servicio. | Candidata a aprobación con la fórmula de atribución exacta del producto y su fecha. |
| INE | El [portal de Datos Abiertos del INE](https://www.ine.es/datosabiertos/) publica sus datos bajo CC BY 4.0, que permite copia, modificación y uso comercial con atribución. | Candidata a aprobación si el conjunto exacto no declara condiciones adicionales. |
| CN IGME-CSIC | La [licencia general de reutilización del IGME](https://info.igme.es/media/ayuda/LicUsoIGME_GENERICA.pdf) permite reutilización y difusión comercial y no comercial. Exige citar «Origen de los datos: ©CN Instituto Geológico y Minero de España (IGME)», autores cuando consten, fecha, metadatos y ausencia de patrocinio; admite condiciones específicas por conjunto. | Candidata a aprobación solo después de revisar el conjunto y sus autores/condiciones específicos. |
| ITACyL | La [LICENCIA-IGCYL-NC](https://ftp.itacyl.es/cartografia/LICENCIA-IGCYL-NC-2012.pdf) autoriza únicamente usos sin aprovechamiento económico directo, indirecto o diferido. Exige atribución visible y aceptación explícita de las mismas condiciones por cada destinatario de una redistribución. | `restricted` hasta confirmar formalmente que el despliegue y todos sus destinatarios son no comerciales y que existe un mecanismo válido de aceptación, o hasta obtener permiso comercial. |
| Dirección General del Catastro | El [WMS catastral](https://www.catastro.hacienda.gob.es/es-ES/wms.html) prohíbe descargas masivas mediante peticiones sucesivas. La [licencia de productos catastrales](https://www.catastro.hacienda.gob.es/pdf/ovc/licdescargaES.pdf) y la [licencia INSPIRE](https://www.catastro.hacienda.gob.es/webinspire/documentos/Licencia.pdf) permiten uso propio y productos de valor añadido transformados, pero prohíben difundir por Internet la información original sin transformación. Una transformación debe producir una obra diferente sin alterar ni desnaturalizar el contenido; además exige atribución y fecha de acceso. | El WMS no se puede usar para una pirámide masiva. La descarga oficial puede conservarse para uso propio, pero el servicio a terceros permanece `restricted` hasta que una revisión cualificada confirme que el producto local constituye una transformación suficiente o se obtenga otra autorización. Reproyectar o cambiar de formato no se da por suficiente automáticamente. |

## Aplicación segura

1. Aplicar el catálogo y generar las fuentes exactas.
2. Revisar las condiciones particulares y la atribución de cada conjunto.
3. Crear un documento `siur-mirror-authorization-v1` por fuente exacta.
4. Ejecutar el dry-run y conservar `review_sha256` y `document_sha256`.
5. Obtener la conformidad explícita de la persona revisora.
6. Aplicar únicamente el documento idéntico confirmando ambos hashes.
7. Repetir la revisión si cambia la definición, el origen, la licencia o el
   uso previsto.

La sincronización puede descubrir y preparar candidatos sin autorización, pero
no debe iniciar red, descargar, materializar, publicar, promover, reactivar,
servir ni revertir una versión cuando falte una revisión vigente que conceda el
permiso concreto.

## Paquete previo para la persona revisora

Antes de redactar autorizaciones se genera un inventario técnico, sin decisiones
preseleccionadas:

```bash
python -m app.reference_layers.mirror_review_packet \
  --provider-key siur > /ruta/siur-review-packet.json
```

El documento usa el esquema `siur-mirror-review-packet-v1`. Separa las fuentes
primarias necesarias para la primera entrega, las alternativas que solo se
usarían como fallback y las capas técnicamente bloqueadas. Para cada fuente
incluye la definición exacta, sus orígenes HTTPS, el servicio y la capa del
catálogo, la estrategia, la evidencia técnica disponible y la cabeza de la
cadena de revisiones.

El paquete no es una autorización y el importador de autorizaciones no lo
acepta. `applicable_as_authorization=false` y todos los campos de
`human_review` permanecen en `null`: decisión, identidad y fecha de la persona
revisora, orígenes permitidos, licencia, términos, atribución y los cinco
permisos. Una licencia indicada por el catálogo o una evidencia técnica se
muestra como contexto y declara expresamente que no concede permiso operativo.

`packet_sha256` liga de forma determinista:

- el snapshot y hash del catálogo;
- las definiciones y los identificadores persistidos de las fuentes;
- la generación y evidencia de estrategias;
- las cabezas de las cadenas de autorización.

No incluye `next_check_at`, de modo que el avance normal del planificador no
invalida una revisión. Antes de convertir las respuestas humanas en documentos
`siur-mirror-authorization-v1`, se vuelve a construir el inventario y se exige
la misma huella:

```bash
python -m app.reference_layers.mirror_review_packet \
  --provider-key siur \
  --expected-packet-sha256 HASH_REVISADO
```

Ambos comandos son de solo lectura: no crean revisiones, no encolan trabajos,
no descargan datos y no realizan peticiones de red. Si el catálogo, una fuente,
la estrategia o la cabeza de autorización cambian, el segundo comando falla
cerrado y hay que revisar un paquete nuevo.
