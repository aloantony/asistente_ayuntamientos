# Publicación municipal del 9 de septiembre de 2026

## Alcance

Mapa con altas atómicas y geometrías; fichas de inventario; edición y asignación de tareas; mantenimiento; herramientas municipales del asistente con permisos y confirmación; paginación y enlaces; diagnóstico del worker y corrección del INE de Fuentelcésped.

Incluye código opcional de Groq y utilidades de cobertura/transferencia. No activa Groq, no importa ordenanzas y no despliega el servicio semántico experimental. Producción mantiene Anthropic sin credencial configurada: el asistente sigue no disponible hasta configurar un proveedor.

## Preparación

Base de producción: d7ea63f, árbol limpio. Migración requerida: 20260904_0045 → 20260905_0046, aditiva (tabla de recibos para evitar duplicados). No reescribirla ni eliminar recibos al revertir aplicación.

Copia privada previa: /home/dev/recuperacion-miconcejo-2026-09-05/produccion-pre-mvp-20260909/daily/20260909T095951Z, base y documentos con SHA256SUMS.

Autorización del usuario: publicar en producción lo que esté listo. Verificación final y resultado del despliegue se registrarán al terminar. Dos fixtures del mapa se actualizan de 09140 a 09137 para reflejar la identidad corregida.

## Pendientes fuera de esta publicación

Procesamiento semántico de 38 fragmentos largos, generación/activación del índice, vinculación documental completa, fuentes autorizadas y actualización de nueve provincias, y activación de un proveedor del asistente. El MVP completo no se considera terminado.
