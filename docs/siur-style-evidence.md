# Evidencia local de estilos SIUR adaptados

## MITECO — zonas inundables

El directorio oficial de MITECO continúa publicando las cinco URLs de estilos
MVT de zonas inundables:

<https://www.miteco.gob.es/es/cartografia-y-sig/ide/directorio_datos_servicios/agua.html>

El 26 de julio de 2026 las cinco URLs respondían `404`. Para que la adaptación
local no dependa de constantes sin procedencia, se conserva en el repositorio
la última captura disponible del documento servido por el dominio oficial.
Cada copia se valida por forma, semántica y SHA-256 antes de construir el SLD.

| Estilo | Captura UTC | SHA-256 del cuerpo archivado | SHA-256 de la copia local | Relleno / borde |
| --- | --- | --- | --- | --- |
| Q10 | `2026-05-11 11:52:07` | `99f7a8c01015ae5cc3983c333e2e76ca30a20502c693fcd25ff6ca878fd99f5b` | `5966ace7d1012605f2bf68264f8533fc73a0a91d4bf08b9b3fba6245d1e77c07` | `#ff0000` / `#c80000` |
| Q50 | `2026-05-11 12:15:30` | `0e753d758c49eded5da62147e8d1329f29359d2864379fa87396ac07fd042868` | `e7d8abd91b0432ede69d2ac2066842d3e961907ebed6884f629cf7c5d6dca193` | `#df73ff` / `#df41ff` |
| Q100 | `2026-05-11 11:45:38` | `5d971fcab264052457976e82d63c8e7392e04b259f0c9944f3271c541fccb592` | `fb4946a17b7bae54d90ef22212586c533fdded5265e30fc901caf2f56d9d37fd` | `#e8beff` / `#b68cff` |
| Q500 | `2026-05-11 12:09:57` | `dfce3a142a2b773762e1381a89a13dcb5523b4c9f35dd5d04d4d2138435bb28f` | `cd1202d3ab3cb98efd688521134a53b72b95863a4a2a792bfc32f80aa07d5744` | `#ff73df` / `#ff32df` |
| ZFP | `2026-05-11 11:48:01` | `18ec64cf71afd41ab622365b0ca67e98fa54417903d1a616e3b868f8371b3e55` | `19dab6437a2d650505333d440cf2cc2996ba055f257c48d0d31921947fc9af8d` | `#cccccc` / `#e6e600` |

Las capturas se obtienen mediante
`https://web.archive.org/web/<timestamp>id_/<url-oficial>`. Se conserva tanto
el hash del cuerpo archivado como el de la copia versionada, que solo difiere
en normalización de espacios y salto final.

Q50 cambió entre la captura del 14 de enero de 2026
(`#ffbee8` / `#a80084`) y la del 11 de mayo de 2026
(`#df73ff` / `#df41ff`). Por ello se usa la captura más reciente y la
adaptación se declara `adapted`, nunca `exact`.

La indisponibilidad del documento remoto no invalida una versión local ya
promocionada. La comprobación periódica debe registrar el error y conservar la
activa; cuando la URL vuelva a servir un documento distinto, sus bytes deben
entrar en `staging` y la nueva adaptación debe revisarse antes de promocionarse.
