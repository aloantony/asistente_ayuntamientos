# Cómo contribuir

Gracias por mirar el código. Antes de nada, dos avisos que ahorran tiempo.

**Licencia.** Este repositorio es de código visible, no de código abierto. Al
enviar una aportación cedes al titular una licencia amplia sobre ella, incluida
la posibilidad de publicarla bajo otra licencia (ver [LICENSE](LICENSE) §4). Si
eso no te encaja, escribe antes de abrir el pull request.

**Alcance.** El proyecto tiene un solo mantenedor y sirve a un ayuntamiento
real en producción. Los cambios grandes sin hablarlos antes suelen acabar
rechazados, no por su calidad, sino porque chocan con una decisión ya tomada.
Abre una incidencia primero y lo hablamos.

## Antes de escribir código

1. Lee [`docs/arquitectura.md`](docs/arquitectura.md) para el estado técnico.
2. Busca tu tema en [`docs/decisiones.md`](docs/decisiones.md). Hay más de sesenta ADR: casi
   todo lo que parece raro está explicado ahí, con el motivo.
3. Levanta el entorno siguiendo el [arranque rápido](README.md#arranque-rápido).

## Reglas que no se negocian

Estas existen porque romperlas tiene consecuencias reales, no estéticas.

- **Nunca edites ni subas `.env` ni `.env.production`.** Las plantillas
  versionadas son `.env.example` y `.env.production.example`.
- **Nunca borres los volúmenes `postgres_data` ni `document_storage`.** Son
  datos de usuario. Eso descarta `docker compose down -v`, `docker volume prune`
  y `docker system prune -a --volumes`. Para recuperar espacio,
  `docker builder prune`.
- **Toda salida a un proveedor de IA pasa por el gateway**
  (`backend/app/assistant/gateway.py`); la voz, sólo por
  `backend/app/assistant/speech.py`; la búsqueda web, sólo por
  `backend/app/assistant/web_search.py`. Ningún otro módulo llama a esos
  servicios. Nunca se envían documentos originales ni ficheros municipales, y en
  los registros van metadatos, nunca contenido.
- **Ningún endpoint nuevo sin comprobar tenencia y permiso.** La cadena es
  usuario → grupo → rol → permiso, con `is_superuser` como excepción.
- **Los bytes de un fichero no entran en la base de datos.** Van al
  almacenamiento con clave generada por el servidor, lista blanca de tipos y
  tope de tamaño; los tres se conservan.
- **PostgreSQL y Redis no se publican nunca.** En desarrollo todo escucha en
  localhost.
- **No introduzcas herramientas nuevas** (linters, formateadores, librerías de
  estado, frameworks de test) sin registrar antes una ADR. Ajústate al estilo del
  código que rodea tu cambio: misma densidad de comentarios, mismos nombres,
  mismos giros.

## Ramas

Trabaja siempre en una rama corta y de un solo tema, nunca directamente sobre
`main`:

```bash
git switch -c feat/descripcion-corta
```

Prefijos en uso: `feat/`, `fix/`, `docs/`, `refactor/`. Para funcionalidades
grandes, encadena varios pull requests pequeños en vez de uno enorme.

## Commits

- **En inglés**, con asunto en imperativo y **sin prefijos de conventional
  commits**. La historia reciente marca el tono: `Stop the map from choking
  itself when it is moved`, `Retire the cartographic mirror`.
- Cuerpo detallado explicando el porqué, no el qué. Si el cambio se apoya en una
  decisión registrada, cítala: `See ADR-012`.
- Los hitos de módulo siguen el patrón `Add <module> v1`.
- Sube al índice sólo lo que pertenece a este cambio. Revisa
  `git diff --cached` antes de confirmar.
- La documentación se sincroniza en commits de documentación propios, después
  del commit de la funcionalidad.

## Decisiones técnicas (ADR)

Si tu cambio elige entre alternativas con consecuencias duraderas —una
dependencia, un modelo de datos, un límite de seguridad, un cambio de topología—
añade una ADR numerada al final de [`docs/decisiones.md`](docs/decisiones.md) y
cítala desde el commit. Una ADR no es un ensayo: contexto, decisión y
consecuencias.

## Migraciones

- Van en `backend/alembic/versions/`, con nombre `YYYYMMDD_NNNN_descripcion.py`
  y **siempre** con `upgrade()` y `downgrade()`.
- Una revisión ya aplicada a una base persistente es inmutable: no se borra, ni
  se renombra, ni se reescribe. Los errores se corrigen con una revisión
  posterior.
- Antes de que una migración borre o transforme datos, comprueba los entornos
  afectados y haz que se detenga con un error claro si no puede resolverlo sola.
- Valida las dos rutas: instalación desde cero y salto desde la última revisión
  desplegada hasta `head`.

## Tests

El backend tiene ~936 tests y el arnés está en `backend/tests/conftest.py`:
sesiones con savepoint y rollback por test (el código de aplicación puede hacer
`commit` libremente), un `client` de TestClient con `get_db` sobrescrito,
fábricas (`make_user`, `make_organization`, `superuser`, `add_member`,
`grant_permissions`) y `headers_for()` para emitir JWT.

- **Un endpoint nuevo necesita tests de aislamiento por organización y de
  control de permisos.** No es opcional.
- **Los tests del asistente nunca llaman a una API de IA real.** Sobrescribe la
  dependencia `get_gateway` con un doble, como en `tests/test_assistant.py`.
- Una corrección de fallo lleva un test de regresión que falle antes del arreglo,
  siempre que sea practicable.
- El frontend usa vitest con `@testing-library/react` sobre jsdom. Los
  componentes de `app/components/` llevan su `*.test.tsx` al lado; los ayudantes
  puros extraídos de un componente se prueban directamente.

## Validación antes de entregar

Pasa esto y pega la salida real en el pull request. No des por buena una
comprobación que no has ejecutado.

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q -o cache_dir=/tmp/pytest_cache"
```

```bash
python3 -m compileall -q backend/app backend/alembic
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
docker compose exec backend alembic current
git diff --check
```

La imagen de backend en ejecución no incluye `pytest` a propósito: el comando de
arriba instala las dependencias de desarrollo en un contenedor desechable. Salvo
que fijes `TEST_DATABASE_URL` a mano, el arnés crea una base `app_test_<uuid>`
única y la destruye al terminar; una base propia debe conservar el prefijo
`app_test`.

## Pull requests

CI (`.github/workflows/ci.yml`) corre en cada pull request: comprobaciones de
repositorio, backend (compilación, migraciones y pytest) y frontend
(typecheck, lint y build). El conjunto `CI gate` es obligatorio para fusionar en
`main`, y la rama debe estar al día.

En la descripción, cuenta qué cambia, por qué, y el plan de pruebas con los
comandos que ejecutaste de verdad. La plantilla te lo pide punto por punto.

## Idioma

- **Interfaz de usuario en español.** Los textos que ve una persona van en
  español.
- **Código, comentarios y commits en inglés.**
- Los `detail` de error de la API se escriben en inglés y se traducen para la
  interfaz en `frontend/app/lib/api.ts` (`translateApiDetail`).
- La documentación de `docs/` va en español.
