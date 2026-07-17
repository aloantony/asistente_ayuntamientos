# Catálogo municipal de Castilla y León

## Cobertura oficial

La referencia administrativa del catálogo es la **Relación de municipios y sus
códigos por provincias del INE a 1 de enero de 2026**, publicada el 4 de febrero
de 2026. El INE construye esta relación con las denominaciones inscritas en el
Registro de Entidades Locales y asigna a cada municipio un código de cinco
dígitos más un dígito de control.

Fuentes revisadas:

- Catálogo vigente: <https://www.ine.es/dyngs/INEbase/es/operacion.htm?c=Estadistica_C&cid=1254736177031&idp=1254735976614&menu=ultiDatos>
- XLSX completo: <https://www.ine.es/daco/daco42/codmun/diccionario26.xlsx>
- Recuento oficial por provincia y comunidad: <https://www.ine.es/daco/daco42/codmun/cod_num_muni_provincia_ccaa.htm>
- Población oficial a 1 de enero de 2025: <https://www.ine.es/dyngs/INEbase/es/operacion.htm?c=Estadistica_C&cid=1254736177011&idp=1254734710990&menu=resultados>

El resultado esperado para Castilla y León es:

| Provincia | Código | Municipios |
|---|---:|---:|
| Ávila | 05 | 248 |
| Burgos | 09 | 371 |
| León | 24 | 211 |
| Palencia | 34 | 191 |
| Salamanca | 37 | 362 |
| Segovia | 40 | 209 |
| Soria | 42 | 183 |
| Valladolid | 47 | 225 |
| Zamora | 49 | 248 |
| **Total** | **07** | **2.248** |

Antes de esta carga, la base de desarrollo auditada contenía 554 códigos INE
oficiales únicos: los 371 municipios de Burgos y los 183 de Soria. Faltaban
1.694 registros oficiales. También había filas de pruebas y tres duplicados sin
código INE; dos de esos duplicados tienen ordenanzas vinculadas y no deben
eliminarse ni fusionarse automáticamente.

## Sincronización reproducible

`app.municipalities.ine_directory` combina dos fuentes con fechas distintas:

- identidad administrativa (código, nombre, provincia y comunidad): catálogo
  INE a 1 de enero de 2026;
- población total: revisión oficial del Padrón a 1 de enero de 2025.

Ambos ficheros están limitados por tamaño y fijados por SHA-256. El importador
valida título, cabeceras, número nacional de filas, total autonómico y recuento
por cada provincia. Las altas y actualizaciones se resuelven exclusivamente por
el código INE de cinco dígitos; nunca se adivinan por similitud de nombre. Si
falta el código canónico pero existe una fila con el mismo nombre y provincia,
o un código no canónico que podría representar el mismo municipio, la ejecución
se bloquea hasta reconciliarla manualmente.

La ejecución sin `--apply` es siempre de solo lectura:

```bash
python -m app.municipalities.ine_directory
```

Después de revisar conflictos, ausencias y filas extra:

```bash
python -m app.municipalities.ine_directory --apply
```

Para un entorno sin salida de red:

```bash
python -m app.municipalities.ine_directory \
  --directory-workbook /datos/diccionario26.xlsx \
  --population-archive /datos/pobmun.zip
```

El proceso es transaccional e idempotente. No borra filas ajenas al catálogo,
no pisa una población oficial más reciente o de otra fuente y exige
`--overwrite-existing` para reemplazar una cifra distinta sin procedencia.

## Información almacenada y límites

La carga completa los datos que las fuentes seleccionadas permiten afirmar con
trazabilidad:

- nombre oficial;
- código INE, dígito de control y procedencia completa del directorio;
- provincia, comunidad autónoma y país;
- tipo `municipality` y estado activo;
- población total, año de referencia, URL de fuente y SHA-256;
- superficie oficial IGN y densidad derivada con la población INE;
- una instantánea NGMEP versionada con perímetro, identificadores `ID_REL` y
  `COD_GEO`, población NGMEP de contraste y hoja MTN25;
- capital municipal con código, nombre, población, longitud, latitud, altitud,
  CRS y orígenes de coordenadas y altitud;
- artefacto fuente con fecha de referencia y recuperación, URLs de catálogo y
  descarga, hashes del ZIP y CSV, licencia y atribución.

El punto NGMEP identifica el núcleo de población de la capitalidad; no es un
centroide geométrico ni el límite del término municipal. La población municipal
incluida en NGMEP se conserva dentro de su instantánea para auditoría, pero no
sustituye la cifra INE canónica: en la versión revisada hay 1.245 diferencias.

No se rellenan códigos postales, perfil rural/urbano, perfil económico, turismo
ni notas mediante inferencias. El XLSX de población también ofrece hombres y
mujeres, pero el modelo actual solo conserva el total; el informe del dry-run
hace visible ese dato no almacenado.

La geografía se carga con:

```bash
python -m app.municipalities.ngmep_geography
python -m app.municipalities.ngmep_geography --apply
```

El primer comando es un dry-run. Ambos verifican los hashes fijados del ZIP y
de `MUNICIPIOS.csv`, la cabecera, los 8.132 municipios nacionales, los 2.248 de
Castilla y León y los nueve recuentos provinciales. El proceso casa únicamente
por código INE, conserva versiones anteriores, bloquea una fuente más antigua o
incoherente y es transaccional e idempotente.

## Evolución recomendada del backend

La procedencia administrativa vigente y la geografía NGMEP ya están modeladas
como datos oficiales verificables. Las siguientes ampliaciones tampoco deberían
consistir en añadir más columnas de texto libre a `municipalities`:

1. Una tabla de indicadores municipales con código de indicador, periodo,
   valor, unidad, dimensiones (por ejemplo sexo o edad), fuente y revisión. Así
   se conserva la serie histórica en vez de sobrescribir una única población.
2. Historial administrativo de altas, bajas, cambios de nombre y alias. La
   carga actual conserva la procedencia vigente, no todos los estados pasados.
3. Límite municipal oficial versionado. El punto de capital ya está separado
   de las ubicaciones operativas, pero no representa una geometría superficial.
4. Códigos postales en una relación estructurada y temporal, no en un campo
   `Text`, porque un municipio puede tener varios y un código puede abarcar más
   de un ámbito.
5. Directorio institucional (web, sede electrónica, DIR3 y contactos) con
   fuente y fecha de verificación; entidades locales menores y núcleos de
   población deben ser entidades relacionadas, no perfiles textuales.
6. Mancomunidades, comarcas y entidades locales menores con identidad propia,
   vigencia y relaciones muchos-a-muchos.

Los perfiles económicos y turísticos pueden mantenerse como resúmenes
editoriales, pero las comparaciones y automatizaciones deben apoyarse en
indicadores oficiales versionados.

## Fuentes de enriquecimiento evaluadas

Se evaluaron dos fuentes oficiales adicionales:

- **Registro de municipios de la Junta de Castilla y León**, actualizado a
  diario y publicado con licencia CC BY 4.0:
  <https://datosabiertos.jcyl.es/web/jcyl/set/es/sector-publico/municipios/1284278782067>.
  Su CSV contiene los mismos 2.248 códigos, población 2025, coordenadas,
  mancomunidades, entidades locales menores y la comarca estatutaria cuando
  aplica. Las mancomunidades y entidades menores son relaciones, no texto de
  perfil: deben importarse desde sus conjuntos propios y con claves estables.
- **NGMEP 2026 del IGN/CNIG**, actualizado el 31 de marzo de 2026 y publicado
  con CC BY 4.0:
  <https://centrodedescargas.cnig.es/CentroDescargas/detalleArchivo?sec=9000004>.
  Aporta `ID_REL`, superficie oficial, perímetro, capital municipal, población
  de la capital, coordenadas ETRS89, origen de coordenadas, altitud y origen de
  altitud. El punto publicado corresponde a la capital o entidad poblacional;
  no debe etiquetarse como centroide geométrico del término municipal. Esta
  fuente ya está implementada mediante versiones de artefacto e instantáneas
  municipales; la Junta sigue siendo candidata para las relaciones restantes.

El encaje implementado para NGMEP es:

- superficie y perímetro como hechos geográficos fechados y con fuente; a
  partir de la superficie se deriva la densidad;
- capital municipal con código y población propios dentro de la instantánea;
- punto de capital en una capa geográfica oficial diferenciada de ubicaciones
  operativas, conservando CRS, origen, altitud, licencia y versión;
- `ID_REL` y códigos geográficos conservados por versión;
- mancomunidades, comarca y entidades locales menores en tablas relacionales;
- presencia de comercio y otros datos sectoriales como indicadores fechados,
  no como perfiles libres.

No se recomienda importar las coordenadas X/Y de la Junta sin el huso y CRS por
fila, ni tratar el código postal del domicilio consistorial como si describiera
todo el término municipal. La web oficial, sede electrónica, DIR3 y contactos
pueden añadirse como directorio institucional opcional con `retrieved_at` y
procedencia propia, sin bloquear el padrón canónico.
