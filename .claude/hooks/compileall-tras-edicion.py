#!/usr/bin/env python3
"""PostToolUse (Edit|Write): comprueba la sintaxis del .py recién editado.

Equivalente por-archivo del `python3 -m compileall` de la validación
pre-handoff (README §9): detecta errores de sintaxis en el momento en vez
de al final. Salida 2 = devolver el error a Claude para que lo corrija.
"""
import json
import py_compile
import sys

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path") or ""

if not path.endswith(".py"):
    sys.exit(0)

try:
    py_compile.compile(path, doraise=True)
except py_compile.PyCompileError as exc:
    print(f"Error de sintaxis tras editar {path}:\n{exc}", file=sys.stderr)
    sys.exit(2)
except OSError:
    sys.exit(0)

sys.exit(0)
