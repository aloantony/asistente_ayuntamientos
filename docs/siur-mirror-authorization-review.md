# Revisión de autorizaciones del espejo SIUR

Estado de contraste: 2026-07-26.

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
