## Qué cambia

<!-- Una o dos frases. Qué hace este cambio y por qué hacía falta. -->

## Contexto

<!-- Incidencia relacionada, ADR que lo sustenta o decisión que lo motiva.
     Si el cambio elige entre alternativas con consecuencias duraderas,
     debería haber una ADR nueva en docs/decisiones.md. -->

## Plan de pruebas

<!-- Los comandos que has ejecutado de verdad, con su resultado.
     Un plan sin salida real no cuenta. -->

```
```

## Comprobaciones

- [ ] Tests del backend en verde
      (`docker compose run --rm -T -v "$(pwd)/backend:/app" backend sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q -o cache_dir=/tmp/pytest_cache"`)
- [ ] `npm --prefix frontend run typecheck`, `lint`, `test` y `build` en verde
- [ ] `git diff --check` limpio
- [ ] Si hay endpoint nuevo: tests de aislamiento por organización y de permisos
- [ ] Si hay migración: `upgrade()` y `downgrade()`, y probada desde cero y desde la revisión desplegada
- [ ] Si toca la salida a IA: sigue pasando sólo por `gateway.py` / `speech.py` / `web_search.py`
- [ ] Documentación actualizada, o va en un commit de documentación aparte
- [ ] No se suben `.env`, secretos ni datos municipales reales
