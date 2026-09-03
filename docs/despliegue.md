# Despliegue en un dominio público

Actualizado: 2026-08-31

Procedimiento para publicar Anacleto en un dominio propio sobre un VPS con Docker.
Decisiones de fondo en ADR-035 (topología y TLS), ADR-036 (endurecimiento) y
ADR-037 (copias). Este documento es la receta; los ADR explican el por qué.

Se asume un **piloto cerrado con datos municipales reales y pocos usuarios**. Los
requisitos formales que quedan pendientes antes de abrir a más ayuntamientos están
en `proteccion-datos.md`.

---

## 0. Antes de empezar: lo que hace falta

| Qué | Estado |
|---|---|
| Servidor | **Ya disponible**: VPS Hetzner CPX42 (8 vCPU, 15 GB, 301 GB, IP pública, UE). Es la misma máquina donde se desarrolla — ver §7 bis |
| Dominio registrado | donDominio (ver §8) |
| Clave de API del runtime de IA | Anthropic u OpenAI, con contrato firmado (ver `proteccion-datos.md`) |
| Backups o snapshots en la consola de Hetzner | **Obligatorio**: son la única copia fuera de la máquina (ADR-037) |

Como el servidor ya existe y ya tiene su usuario, SSH y Docker en marcha, el paso 1
es en gran parte una verificación, no una instalación.

Dos decisiones previas: **qué estado del código se despliega** (§7) y las
salvaguardas por compartir máquina con el desarrollo (§7 bis).

---

## 1. Revisar el servidor

El servidor ya está en marcha con Docker y con el desarrollo encima, así que este
paso es **verificar y endurecer**, no instalar. Lo comprobado el 2026-07-30:

- Puertos 80 y 443 libres. ✔
- Único servicio expuesto al exterior: SSH (22). Todo lo demás escucha en
  loopback. ✔
- IPv4 `178.105.228.93` e IPv6 `2a01:4f8:1c0c:5ccb::1` directas en la interfaz. ✔

Lo que queda por endurecer:

```bash
# Acceso: solo clave, nunca contraseña.
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sshd -t && sudo systemctl reload ssh
```

> Antes de cerrar la sesión actual, **abre otra terminal y comprueba que puedes
> entrar**. Si algo va mal, la sesión abierta es la única forma de arreglarlo.

Cortafuegos y actualizaciones automáticas:

```bash
sudo apt install -y ufw unattended-upgrades fail2ban
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH        # PRIMERO, o te quedas fuera
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo systemctl enable --now fail2ban
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

> **Aviso sobre Docker y ufw.** Docker escribe sus propias reglas de iptables y
> los puertos que publica **se saltan ufw**. Aquí eso no abre nada porque sólo
> Caddy publica puertos: Postgres y Redis no publican ninguno, y los servicios de
> desarrollo están atados a loopback. Por eso importa no «publicarlos un momento
> para depurar»: eso sí los pondría en Internet, saltándose el cortafuegos.

`docker` ya arranca con el sistema, y con `restart: unless-stopped` en el Compose
eso basta para recuperar el servicio tras un reinicio. No hace falta unidad systemd
para el stack.

---

## 2. Traer el código y los secretos

Producción vive en `/opt/anacleto`, en un **clon distinto del árbol de desarrollo**
(§7 bis explica por qué). Como el repositorio está en la misma máquina, el clon
sale de ahí:

```bash
sudo install -d -o "$USER" -g "$USER" /opt/anacleto
git clone /home/dev/proyectos/asistente_ayuntamientos /opt/anacleto
cd /opt/anacleto
git checkout <rama-o-etiqueta-que-se-despliega>
```

A partir de aquí, **todos los comandos de producción se ejecutan desde
`/opt/anacleto`**, nunca desde el directorio de desarrollo.

Fichero de entorno de producción, **solo en el servidor**:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

Genera cada secreto y pégalo en el fichero:

```bash
openssl rand -base64 48   # SECRET_KEY
openssl rand -base64 32   # POSTGRES_PASSWORD (y la misma dentro de DATABASE_URL)
openssl rand -base64 32   # BOOTSTRAP_ADMIN_TOKEN
```

La plantilla ya viene con el dominio puesto (`PUBLIC_HOSTNAME=www.miconcejo.es`,
`APEX_HOSTNAME=miconcejo.es`, CORS y `ALLOWED_HOSTS` en coherencia). Queda rellenar
los tres secretos, `ACME_EMAIL` y la clave del runtime de IA. El backend **se niega a arrancar**
en producción con la clave de ejemplo, con orígenes en claro o con
`ALLOWED_HOSTS=*` (ADR-036): si falla al levantar, lee el mensaje, es literal.

> `.env.production` está en `.gitignore`. No se commitea nunca, igual que `.env`.

---

## 3. Levantar el stack

El DNS ya tiene que apuntar aquí (§8) para que Caddy pueda emitir el certificado.

```bash
cd /opt/anacleto
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml exec backend alembic upgrade head
```

Comprueba que todo está sano:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml ps
curl -fsS https://www.miconcejo.es/api/health
curl -fsS https://www.miconcejo.es/api/ready     # comprueba base de datos y Redis
```

Si vas a probar el certificado varias veces, descomenta primero `acme_ca` en
`ops/Caddyfile` para usar el entorno de pruebas de Let's Encrypt: los cupos de
emisión son semanales y se agotan.

### Volumen de documentos reutilizado

Los contenedores ya **no corren como root** (ADR-036). Si el volumen
`document_storage` viene de un despliegue anterior, sus ficheros serán de otro
usuario y las subidas fallarán con «permission denied». Corrección puntual, que
solo cambia la propiedad y **no toca el contenido**:

```bash
docker run --rm -u 0 -v anacleto_document_storage:/data alpine \
  chown -R 10001:10001 /data
```

Nunca hay que borrar `postgres_data` ni `document_storage`.

---

## 4. Crear el primer superusuario y retirar el token

```bash
curl -X POST https://www.miconcejo.es/api/auth/bootstrap-admin \
  -H "Content-Type: application/json" \
  -H "X-Bootstrap-Admin-Token: <BOOTSTRAP_ADMIN_TOKEN>" \
  -d '{"email":"alcalde@ejemplo.es","password":"<contraseña larga>","full_name":"Nombre Apellidos"}'
```

**Paso obligatorio a continuación:** borra la línea `BOOTSTRAP_ADMIN_TOKEN` de
`.env.production` y reinicia el backend. El endpoint pasa a devolver 503 y deja de
ser una puerta:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d backend
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://www.miconcejo.es/api/auth/bootstrap-admin  # 503
```

---

## 4 bis. Cartografía SIUR en el mapa municipal

El mapa de `/mapa` se sirve por **dos vías distintas**, y hacen falta las dos:

- **Las capas temáticas de IDECyL, por proxy.** El navegador nunca habla con
  `idecyl.jcyl.es`, sólo con nuestra API, que pide la tesela y la cachea en
  Redis (ADR-055).
- **El fondo del mapa, desde el espejo local.** Las capas de fondo son del IGN,
  no de la Junta, y el proxy sólo habla WMS contra `idecyl.jcyl.es`. Sin espejo,
  las capas temáticas flotan sobre un cuadriculado vacío (ADR-057).

El espejo **no necesita GeoServer**: sembrar escribe un MBTiles y servir sólo lo
lee. Producción no monta GeoServer y aun así sirve el fondo desde su propio
disco.

### El proxy de IDECyL

Falla cerrado por diseño. Hacen falta las cuatro piezas, en este orden:

1. **Catálogo promovido.** Sin una instantánea `applied` y vigente,
   `/reference-layers/catalog` responde 503 y el mapa no lista ninguna capa.
   `settings.json` de SIUR cambia con el tiempo, así que la promoción se revisa
   cada vez y no se puede copiar la de desarrollo:

   ```bash
   docker compose --env-file .env.production -f docker-compose.prod.yml \
     exec backend python -m app.reference_layers.siur_sync \
     --settings /ruta/settings.json --wmc /ruta/wmc.xml
   ```

   El dry-run imprime conteos y hashes; para `--apply` hay que repetirlos con
   las opciones `--approved-*`.

2. **Revisión humana de licencia, por servicio.** Un documento
   `siur-license-review-v1` firmado por una persona identificada. No se aprueba
   automáticamente: sin él la atestación es de revocación y no se sirve nada.
   El análisis del aviso legal de la Junta está en
   `siur-mirror-authorization-review.md`.

3. **Evidencia de entrega importada**, con los cinco hashes aprobados:

   ```bash
   docker compose --env-file .env.production -f docker-compose.prod.yml \
     exec backend python -m app.reference_layers.siur_delivery_import \
     --capabilities /ruta/GetCapabilities.xml \
     --license-review /ruta/license-review.json
   ```

   El GetCapabilities se descarga **a mano** y se revisa antes; el importador
   sólo lee ficheros locales y nunca acepta una URL. Pídelo en **1.3.0**: el
   1.1.1 de GeoServer lleva DOCTYPE y el parser prohíbe DTD por defensa XXE.

4. **El opt-in del operador**, en `.env.production`:

   ```
   REFERENCE_REMOTE_PROXY_ENABLED=true
   ```

   Es lo último que se activa, y basta con recrear el backend
   (`up -d backend`), sin `down`.

### El espejo del fondo

Tres piezas de despliegue, ya recogidas en `docker-compose.prod.yml`:

- el volumen `reference_artifacts`, montado `:ro` en el backend — es el archivo
  que la API lee para servir cada tesela;
- `group_add` con `REFERENCE_STORAGE_GID` en el backend, porque el worker
  escribe el archivo como `root:<gid>` con 0640 y sin ese grupo la API no puede
  leer lo que ella misma sirve: **todas las teselas del espejo salen 502**;
- el volumen `reference_transient`, para el trabajo intermedio de la siembra.

Y tres pasos de operación:

1. **Reconciliar las fuentes** contra el catálogo promovido:

   ```bash
   docker compose --env-file .env.production -f docker-compose.prod.yml \
     exec backend python -m app.reference_layers.mirror_reconcile --apply
   ```

2. **Autorización de espejo, por fuente.** Un documento
   `siur-mirror-authorization-v1`, distinto y más exigente que la revisión de
   licencia del proxy: copiar obliga a más que servir de intermediario. El fondo
   es del IGN, y son sus propios servicios los que declaran `CC BY 4.0 scne.es`
   en el `AccessConstraints` de sus capacidades — mirar ahí antes que cualquier
   tabla de productos. Primaria y respaldo usan protocolos distintos
   (`wms_tiles` y `wmts`), así que necesitan **una autorización cada una**.

3. **Sembrar.** El servicio `backend` monta el archivo en sólo lectura y la red
   `data` es interna, sin salida a Internet, así que la siembra va en un
   contenedor de un solo uso conectado a las dos redes:

   ```bash
   docker create --name anacleto-siembra -u 0:2000 --env-file .env.production \
     --network anacleto_data \
     -v anacleto_reference_artifacts:/var/lib/asistente_ayuntamientos/reference-artifacts \
     -v anacleto_reference_transient:/var/lib/asistente_ayuntamientos/reference-transient \
     anacleto-backend python -m app.reference_layers.mirror_runtime worker --once
   docker network connect anacleto_edge anacleto-siembra
   docker start -a anacleto-siembra
   ```

   El perfil municipal son unas 24.500 teselas y unos 400 MB: siete minutos, a
   unas 171 teselas por minuto. `tile_seed` **no reintenta**, así que un solo
   502 del origen tumba la siembra entera y hay que repetirla.

### Comprobación

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://www.miconcejo.es/api/health
```

Con sesión iniciada, `/mapa` debe listar el árbol de capas y pintar las teselas.
Si el árbol aparece pero las capas salen deshabilitadas, el bloqueo lo dice el
propio catálogo: `remote_proxy_disabled` es el punto 4, `attestation_missing` el
3, y `license_not_approved` el 2. Para las capas de fondo el bloqueo es del
espejo: `mirror_authorization_missing` es su punto 2 y `local_not_ready`
significa autorizada pero todavía sin sembrar.

### Lo que IDECyL no ofrece

Sus servicios no anuncian `GetLegendGraphic` ni `application/json` en
`GetFeatureInfo`, así que **leyendas e identificación no están disponibles**. Es
una limitación del origen, no un fallo del despliegue. Las teselas sí funcionan.

---

## 5. Copias de seguridad

```bash
sudo cp ops/anacleto-backup.service ops/anacleto-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anacleto-backup.timer
systemctl list-timers anacleto-backup.timer
```

Primera copia a mano y **ensayo de restauración**, que es lo que convierte la copia
en algo real:

```bash
sudo ops/backup.sh
sudo ops/restore.sh --list
sudo ops/restore.sh --from daily/<sello-temporal>     # restaura a app_restore_check, no toca producción
```

El ensayo imprime el recuento de filas de la base restaurada. Compáralo con
producción. Repite el ensayo cada vez que cambie el esquema de forma relevante.

Restauración real (destructiva, pide confirmación escrita):

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml stop backend worker
sudo ops/restore.sh --from daily/<sello> --production --with-documents
docker compose --env-file .env.production -f docker-compose.prod.yml start backend worker
```

Y en el panel del VPS: **activa los snapshots automáticos** y anota su frecuencia
y retención. Sin ellos no hay ninguna copia fuera de la máquina (ADR-037).

---

## 6. Operación diaria

```bash
# Estado y salud
docker compose --env-file .env.production -f docker-compose.prod.yml ps

# Logs (rotados a 10 MB × 5 por servicio)
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f backend

# Copias
journalctl -u anacleto-backup.service -n 50

# Traza de seguridad: intentos de login fallidos y bloqueos
curl -s https://www.miconcejo.es/api/admin/security-events?event_type=auth.login.failed \
  -H "Authorization: Bearer <token de superusuario>"
```

Actualizar a una versión nueva del código. El `git pull` da por supuesto que el
clon **no tiene modificaciones locales**: si `git status` muestra alguna, es que un
arreglo aplicado a mano nunca llegó a `main`, y el pull lo machacaría. Llévalo al
repositorio antes de actualizar.

```bash
cd /opt/anacleto
git status --short          # debe salir vacío
git pull
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml exec backend alembic upgrade head
```

Cambiar de dominio obliga a **reconstruir el frontend**: la URL de la API se hornea
en el bundle del navegador durante el build (ADR-035).

---

## 7. Qué estado del código se despliega

Punto a decidir antes del despliegue, no después. En el momento de escribir esto,
la base de datos de desarrollo `app` está migrada a una revisión que **solo existe
en ramas `codex/*` sin fusionar**, y además tiene la extensión `postgis` registrada,
que la imagen `pgvector/pgvector` no puede cargar: `pg_dump` de esa base **falla**.

Consecuencias prácticas:

- Producción debe partir de un estado de código **coherente y fusionado**, con su
  cadena de migraciones completa desde cero.
- Si en algún momento se quiere llevar datos de la base de desarrollo a producción,
  antes hay que resolver `postgis`: o se añade al servicio de Postgres una imagen
  que lo incluya, o se retira la extensión de esa base. Hasta entonces esa base no
  se puede respaldar ni restaurar con la imagen actual.
- Lo limpio para el piloto es **empezar con base vacía** y crear los datos reales
  desde la aplicación.

**Resuelto el 2026-08-31 por la primera vía**: el servicio `postgres` de
`docker-compose.prod.yml` ya no usa el tag de `pgvector`, sino la imagen que
construye `./postgres` (PG17 + pgvector 0.8.2 + PostGIS 3.6.4), la misma de
desarrollo y CI. La cadena de migraciones se aplica entera desde cero y una base
con la extensión `postgis` se puede respaldar y restaurar. El piloto arrancó de
todos modos con base vacía, que es lo que se hizo el 2026-08-25.

---

## 7 bis. Convivencia con el desarrollo en la misma máquina

El piloto se despliega **en el mismo VPS donde se desarrolla** (Hetzner CPX42:
8 vCPU, 15 GB de RAM, 301 GB de disco, IP pública propia). Es viable —los
contenedores de la aplicación consumen menos de 300 MB y los puertos 80/443 están
libres— pero convive con más de veinte contenedores de otros proyectos, y eso
introduce riesgos que no existirían en una máquina dedicada. Estas salvaguardas no
son recomendaciones: son la condición para que sea aceptable.

### Aislamiento

- **Producción se despliega desde un clon aparte**, `/opt/anacleto`, nunca desde el
  árbol de trabajo de desarrollo. Si compartieran directorio, un `git checkout` a
  otra rama cambiaría el Caddyfile y el Compose de la web en marcha, y un
  `docker compose down -v` tecleado en el directorio equivocado se llevaría los
  datos del ayuntamiento.
- **Proyecto de Compose distinto**: producción es `anacleto` (lo fija `name:` en
  `docker-compose.prod.yml`) y desarrollo es `asistente_ayuntamientos`. Los
  volúmenes son por tanto `anacleto_postgres_data` y `anacleto_document_storage`,
  separados de los de desarrollo.
- **Sin choque de puertos**: desarrollo escucha en 127.0.0.1:8000 y 127.0.0.1:3000;
  producción sólo publica 80 y 443 a través de Caddy.

### Las dos reglas de las que depende no perder los datos

**1. En producción se usa `stop`/`start`, nunca `down`.**

```bash
# Mantenimiento:
docker compose --env-file .env.production -f docker-compose.prod.yml stop backend worker
docker compose --env-file .env.production -f docker-compose.prod.yml start backend worker
```

`down` elimina los contenedores. Un volumen al que ningún contenedor apunta queda
huérfano y pasa a ser candidato de cualquier poda posterior. Mientras el contenedor
exista —aunque esté parado— su volumen es intocable para `prune`.

**2. Para recuperar disco, `builder prune`; jamás `system prune -a --volumes`.**

```bash
docker builder prune -f          # seguro: sólo caché de construcción
docker image prune -a            # seguro: sólo imágenes sin contenedor
docker system prune -a --volumes # ¡NO! puede borrar volúmenes huérfanos
```

Esto importa porque la caché de construcción de esta máquina ronda los 75 GB
recuperables, así que la tentación de podar aparece de forma natural cuando el
disco aprieta. `builder prune` recupera casi todo ese espacio sin tocar ningún
volumen.

Comprobación de espacio antes de decidir:

```bash
docker system df
df -h /
```

### Copias fuera de la máquina: aquí dejan de ser opcionales

Con producción y desarrollo en el mismo disco, las copias locales protegen frente a
un error de la aplicación, pero **no** frente a perder la máquina. En la consola de
Hetzner Cloud hay que activar:

- **Backups automáticos** del servidor (opción de pago, ~20 % del precio del VPS,
  con varias copias rotadas), o
- **Snapshots manuales** antes de cada cambio importante.

Sin una de las dos, la única copia del ayuntamiento vive en el mismo disco que se
está intentando proteger.

### Lo que sigue siendo peor que una máquina dedicada

Conviene tenerlo escrito para poder revisarlo más adelante: el trabajo de
desarrollo (compilaciones, tests, navegador, agentes) compite por CPU y memoria con
la web pública; un reinicio para actualizar el núcleo tira el sitio; y un error
humano en la máquina afecta a las dos cosas a la vez. Cuando el piloto deje de ser
piloto, lo sensato es mover producción a un VPS propio, que en Hetzner cuesta unos
4 €/mes.

## 8. Qué hay que hacer en donDominio

Con el VPS ya creado y su IP conocida.

Dominio: **miconcejo.es**, con **`www.miconcejo.es` como canónico**. El dominio a
secas redirige al canónico, y ambos tienen que resolver a este servidor porque
Caddy emite un certificado para cada uno.

Datos de este servidor:

- IPv4: `178.105.228.93`
- IPv6: `2a01:4f8:1c0c:5ccb::1`

### Primero: quitar el aparcamiento

En el momento de escribir esto el dominio está **aparcado en donDominio**:

```
miconcejo.es      A     -> 31.214.178.55        (parking)
www.miconcejo.es  CNAME -> parkingsrv0.dondominio.com
```

Hay que **borrar el `CNAME` de `www` a `parkingsrv0.dondominio.com`** y desactivar
la redirección web o «página en construcción» del panel. Mientras ese CNAME siga,
`www` apunta a los servidores de donDominio, la web no llega aquí y la validación
del certificado falla. Un `CNAME` además no puede convivir con registros `A`/`AAAA`
en el mismo nombre, así que sustituirlo no es opcional.

### Registros que hay que dejar

Los nameservers ya son los de donDominio (`ns1`/`ns2.dondominio.com`), así que la
zona se edita en su propio panel; no hace falta delegar a ningún sitio.

| Tipo | Nombre | Valor | TTL |
|---|---|---|---|
| `A` | `@` | `178.105.228.93` | 300 durante el cambio, luego 3600 |
| `AAAA` | `@` | `2a01:4f8:1c0c:5ccb::1` | 300 → 3600 |
| `A` | `www` | `178.105.228.93` | 300 → 3600 |
| `AAAA` | `www` | `2a01:4f8:1c0c:5ccb::1` | 300 → 3600 |
| `CAA` | `@` | `0 issue "letsencrypt.org"` | 3600 |

El `CAA` declara que sólo Let's Encrypt puede emitir certificados para el dominio;
hoy no hay ninguno, así que cualquier autoridad podría hacerlo.

Bajar el TTL a 300 **antes** de tocar nada ahorra esperas: si se cambia con el TTL
alto por defecto, los resolutores pueden seguir sirviendo la IP del parking durante
horas.

### Correo, aunque la aplicación no envíe ninguno

Convine cerrar la puerta para que nadie suplante el dominio:

| Tipo | Nombre | Valor |
|---|---|---|
| `TXT` | `@` | `v=spf1 -all` |
| `TXT` | `_dmarc` | `v=DMARC1; p=reject; rua=mailto:<tu correo>` |

Si más adelante se contrata correo en el dominio, estos dos registros hay que
rehacerlos con los datos del proveedor.

### Cuenta del registrador

- **Verificación en dos pasos** activada.
- **Bloqueo de transferencia** (registrar lock) activado.
- Privacidad de WHOIS activada.
- Correo de contacto del dominio vigente: si caduca sin avisar, se cae la web.

### Comprobación de que el DNS ya está bien

```bash
dig +short A     miconcejo.es          # -> 178.105.228.93
dig +short A     www.miconcejo.es      # -> 178.105.228.93 (y NINGÚN CNAME de parking)
dig +short AAAA  www.miconcejo.es      # -> 2a01:4f8:1c0c:5ccb::1
dig +short CNAME www.miconcejo.es      # -> vacío
dig +short CAA   miconcejo.es          # -> 0 issue "letsencrypt.org"
```

Cuando los cinco cuadren, levanta el stack (§3). Caddy pedirá los dos certificados
—`miconcejo.es` y `www.miconcejo.es`— en el primer arranque, y después los renueva
solo. Si vas a repetir el arranque varias veces mientras ajustas algo, descomenta
antes `acme_ca` en `ops/Caddyfile` para usar el entorno de pruebas de Let's
Encrypt: los cupos de emisión son semanales.
