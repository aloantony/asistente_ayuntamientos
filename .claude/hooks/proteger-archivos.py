#!/usr/bin/env python3
"""PreToolUse (Edit|Write): bloquea la edición de archivos protegidos.

- `.env` nunca se edita ni se commitea (regla dura de CLAUDE.md); la
  plantilla documentada es `.env.example`, que sí es editable.
- `package-lock.json` solo debe cambiar a través de npm, nunca a mano.

Salida 2 = bloquear la herramienta; el mensaje en stderr vuelve a Claude.
"""
import json
import re
import sys

PROTECTED = re.compile(r"(^|/)\.env$|(^|/)package-lock\.json$")

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path") or ""

if PROTECTED.search(path):
    print(
        f"Archivo protegido: {path}. `.env` no se edita nunca (la plantilla es "
        "`.env.example`); `package-lock.json` solo cambia vía npm.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
