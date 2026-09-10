# Política de seguridad

Este proyecto maneja datos de administraciones públicas españolas. Los avisos de
seguridad se agradecen y se atienden.

## Qué versión se mantiene

Sólo `main`. No hay versiones publicadas ni ramas de soporte: el arreglo se
aplica sobre `main` y se despliega desde ahí.

## Cómo avisar de un fallo

**No abras una incidencia pública.** Usa el aviso privado de GitHub:

> Pestaña **Security** del repositorio → **Report a vulnerability**

Eso abre un hilo privado entre quien avisa y el mantenedor, con posibilidad de
publicar después un aviso con CVE.

Cuenta con un acuse de recibo en **5 días laborables**. El proyecto lo mantiene
una sola persona, así que el plazo de arreglo depende de la gravedad: un fallo
que exponga datos de un ayuntamiento va primero, siempre.

No hay programa de recompensas. Sí hay crédito público en el aviso, si lo
quieres.

## Qué incluir en el aviso

- Qué falla y qué impacto tiene: a qué datos o acciones da acceso.
- Cómo reproducirlo, paso a paso, con la petición o el fragmento mínimo.
- Versión: el `commit` sobre el que lo has visto.
- Configuración relevante: runtime del asistente, si iba con Docker Compose de
  desarrollo o de producción, qué permisos tenía tu usuario.

## Alcance

**Dentro:** el código de este repositorio. Interesan especialmente los fallos en
las fronteras que sostienen el diseño:

- **Aislamiento por organización.** Cualquier vía por la que un usuario de una
  organización alcance datos de otra.
- **Control de acceso.** Un endpoint que no compruebe permiso, una elevación a
  `is_superuser`, un ámbito de `users.manage` más ancho de lo debido.
- **Fuga por el gateway de IA.** Que salga hacia un proveedor algo distinto de
  texto de conversación, memoria institucional aprobada o campos escritos por el
  usuario. Documentos originales y ficheros municipales no deben salir nunca.
- **Almacenamiento de documentos.** Salto de directorio, saltarse la lista blanca
  de tipos o el tope de tamaño, acceso a un fichero de otro proyecto.
- **Lector web e inyección indirecta de instrucciones.** El contenido web y el de
  los adjuntos es dato, nunca instrucción; una vía para que el modelo obedezca a
  una página es un fallo de seguridad.
- **Sesión y autenticación.** Cookie, CSRF de origen, limitación de intentos de
  acceso, `bootstrap-admin`.
- **Inmutabilidad de `security_events`.** Cualquier forma de alterar o borrar
  esa tabla sin pasar por la purga de retención declarada.

**Fuera:**

- Ataques de denegación de servicio por volumen.
- Fallos que exijan acceso físico o `root` en la máquina anfitriona.
- La ausencia de una cabecera o de un endurecimiento cuando no lleva a un
  impacto demostrable.
- Vulnerabilidades en dependencias de terceros sin explotación demostrada en
  este código: repórtalas al proyecto correspondiente.
- Los límites ya documentados como pendientes en
  [`docs/proteccion-datos.md`](docs/proteccion-datos.md) y en el README, como
  que los limitadores de caudal viven en memoria por proceso y por eso
  producción va con un solo worker. Están reconocidos; si encuentras una forma
  de explotarlos que no esté descrita, eso sí interesa.

## Cómo probar

Levanta tu propia instancia local con el
[arranque rápido](README.md#arranque-rápido) y pruébala ahí.

**No lances pruebas contra el despliegue en producción ni contra el
ayuntamiento que lo usa.** Es un servicio público real con datos reales de
vecinos. Un escaneo, una prueba de fuerza bruta o una carga de datos contra esa
instancia no es investigación de seguridad y se tratará como lo que es.

## Contexto

La postura de protección de datos del piloto, las bases de licitud, los
encargados de tratamiento y las puertas que siguen abiertas están en
[`docs/proteccion-datos.md`](docs/proteccion-datos.md).

---

**English.** Report vulnerabilities privately through the repository's
**Security** tab → **Report a vulnerability**. Do not open a public issue and do
not test against the production deployment: run your own local instance. Scope,
priorities and exclusions are described above; the data-protection posture is in
[`docs/proteccion-datos.md`](docs/proteccion-datos.md) (Spanish).
