# Despliegue

Cómo poner la aplicación en un servidor alcanzable desde internet. El
repositorio trae lo que puede traerse; este documento marca con claridad lo que
no puede decidirse desde el código.

## 1. Lo que tienes que decidir tú

Cuatro cosas, y ninguna la puede resolver el repositorio:

1. **Dónde se aloja.** Un servidor con Docker y Docker Compose. Todo lo demás
   asume que existe.
2. **Un dominio.** La cookie de sesión es `SameSite=Lax` y de host único
   (ADR-010): frontend y backend deben servirse bajo el mismo nombre.
3. **Certificados TLS.** No son opcionales, ver §4.
4. **Los valores reales de los secretos.** Se generan una vez y no se guardan
   en el repositorio.

## 2. Requisitos del servidor

- Docker y Docker Compose.
- Un proxy inverso en el mismo host (nginx, Caddy, Traefik: da igual cuál).
- Volúmenes persistentes para `postgres_data` y `document_storage`. **Nunca se
  borran**: son datos de usuario.

## 3. Configuración

Se parte de `.env.example`. En producción hay cuatro valores que la aplicación
**se niega a arrancar** si conservan su valor de desarrollo (ADR-047):

```bash
ENVIRONMENT=production
SECRET_KEY=<32 caracteres o más, aleatorios>
DATABASE_URL=postgresql+psycopg://app:<contraseña real>@127.0.0.1:5432/app
CORS_ALLOWED_ORIGINS=https://<tu-dominio>
```

Para generar la clave de firma:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`SECRET_KEY` firma los JWT, las autorizaciones de herramientas y las firmas del
catálogo de ordenanzas. Cambiarla más adelante invalida todas las sesiones
abiertas; no es catastrófico, pero conviene hacerlo antes de tener usuarios.

`CORS_ALLOWED_ORIGINS` puede quedar **vacío** si frontend y backend se sirven
bajo el mismo origen, que es el caso recomendado: entonces no hay ninguna
petición cross-origin que permitir.

`BOOTSTRAP_ADMIN_TOKEN` se deja **sin definir** salvo durante el arranque
inicial (§6). Sin él, el endpoint que crea el primer superusuario está cerrado.

Si el asistente debe funcionar, hace falta además la clave del runtime elegido
(`ANTHROPIC_API_KEY` para el valor por defecto). Sin ella los endpoints del
asistente devuelven 503 por diseño: la aplicación arranca, pero su función
principal queda apagada.

## 4. Proxy inverso y TLS

Fuera de `development`, la cookie de sesión se emite con el atributo `Secure`.
El navegador entonces **no la envía por HTTP**, así que sin TLS el login no
falla con un error claro: simplemente no inicia sesión nunca. Esto no es una
recomendación de seguridad que se pueda posponer; sin HTTPS la aplicación no
funciona.

El proxy debe:

- terminar TLS para `https://<tu-dominio>`;
- enviar `/api` al backend en `127.0.0.1:8000`;
- enviar el resto al frontend en `127.0.0.1:3000`;
- reenviar `X-Forwarded-Proto: https`.

Ambos servicios escuchan solo en loopback: el proxy es lo único que los
alcanza.

## 5. Primer despliegue

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Y aplicar las migraciones:

```bash
docker compose exec backend alembic upgrade head
```

La aplicación **no** las aplica sola al arrancar: hacerlo sería ejecutar
migraciones desde varios contenedores a la vez. Es un paso explícito, también
en cada actualización.

## 6. Crear el primer superusuario

Con la base vacía no hay ninguna cuenta. Se define `BOOTSTRAP_ADMIN_TOKEN` con
un valor propio, se reinicia el backend, se llama una vez a
`POST /auth/bootstrap-admin` con la cabecera `X-Bootstrap-Admin-Token`, y
después **se quita la variable y se reinicia**. El endpoint solo responde
mientras no exista ningún usuario, pero dejar el token puesto no aporta nada.

## 7. Actualizaciones

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose exec backend alembic upgrade head
```

Las revisiones de Alembic están serializadas (ADR-033): siempre hay una sola
cabeza y `upgrade head` es inequívoco.

## 8. Instalaciones anteriores: propiedad del volumen de documentos

Los contenedores ya no corren como root. Un volumen `document_storage` **creado
antes** de ese cambio sigue perteneciendo a root, y el backend no podrá
escribir en él. Se corrige una sola vez:

```bash
docker compose run --rm --user root backend chown -R app:app /var/lib/asistente_ayuntamientos/documents
```

En una instalación nueva no hace falta: el volumen hereda la propiedad correcta
de la imagen.

## 9. Lo que sigue abierto

Honestamente, para que nadie lo descubra en producción:

- **Los limitadores de credenciales ya son distribuidos** (login y cambio de
  contraseña, ADR-048): viven en Redis y `ENVIRONMENT=production` exige
  `RATE_LIMIT_BACKEND=redis`. Escalar a varios workers ya no los desactiva,
  pero la clave sigue siendo cliente+cuenta, así que el password spraying
  desde muchas IP contra una sola cuenta sigue sin tope (ADR-015). Los topes
  del proxy WMS sí son por proceso y con varios workers contarían por worker.
- **El logout no invalida el JWT**, que caduca a los 60 minutos (ADR-015). La
  revocación en servidor solo cubre el cambio y el reinicio de contraseña.
- **El guard de sesión del frontend es de cliente.** Las rutas se protegen en
  el backend; el frontend solo evita enseñar lo que no toca.
- **No hay copias de seguridad automatizadas.** `postgres_data` y
  `document_storage` son datos de usuario y nadie los respalda por ti.
