# Visión de producto: Anacleto

Actualizado: 2026-07-17.

Estado: **fuente principal de las decisiones de producto objetivo**. Describe el
producto que se quiere construir, no afirma que todas estas capacidades estén ya
implementadas. `docs/arquitectura.md` describe el estado técnico actual; el
roadmap de `README.md` resume la transición entre ambos.

## 1. Definición

> Anacleto es un agente operativo municipal que comprende el contexto y las
> competencias de cada usuario, utiliza los sistemas autorizados, coordina
> personas y ejecuta trabajo de forma autónoma dentro de límites explícitos,
> trazables y revocables.

El producto es Anacleto y su capacidad de completar trabajo. Documentos, tareas,
agenda, expedientes, normativa, proyectos o mapas son capacidades y superficies
de trabajo que Anacleto utiliza; no son productos independientes que compitan por
ser la entrada principal.

## 2. Usuarios y dominio

- El primer usuario es el alcalde.
- El producto se extenderá al resto de cargos y trabajadores del ayuntamiento.
- Cada usuario conversa directamente con Anacleto.
- Anacleto puede abordar cualquier trabajo relacionado con el dominio y las
  competencias del usuario, no solo una lista cerrada de casos de uso.
- Los escenarios concretos se utilizarán para evaluar calidad y seguridad, no
  para limitar permanentemente el alcance del producto.
- El dominio incluye administración, gobierno, análisis y estrategia
  institucional del ayuntamiento.
- Quedan fuera la actividad personal, partidista y electoral.

La atención ciudadana no forma parte de la primera etapa. Si se incorpora en el
futuro, será un asistente separado, con identidad, memoria, permisos y
herramientas propios, y solo accederá a información expresamente publicada.

## 3. Experiencia principal

La conversación por texto y voz es la entrada y el hilo conductor. No será la
única representación de la información: Anacleto abrirá vistas específicas para
revisar documentos, tareas, agenda, expedientes, permisos, actividad o cualquier
artefacto complejo.

El usuario podrá modificar esos artefactos visualmente y continuar después la
conversación sin perder el contexto. Las vistas deben ayudar a comprender,
comparar, aprobar o corregir; la navegación entre módulos no debe convertirse en
el trabajo principal del usuario.

El lienzo documental es la primera superficie contextual implementada: permite
desarrollar con Anacleto un Markdown privado y versionado junto al chat. Sigue
siendo un espacio de trabajo no oficial; aprobación, publicación o incorporación
a un expediente requieren contratos distintos.

## 4. Un Anacleto por ayuntamiento

Cada ayuntamiento tendrá un único Anacleto organizacional y una instancia propia
del producto. Todos los usuarios comparten el mismo sistema de conocimiento y
trabajo, pero cada uno solo accede a la información permitida por su identidad,
cargo, competencias, departamento y delegaciones.

No habrá una base de datos operativa compartida entre ayuntamientos. La
arquitectura separará:

- La instancia municipal, que contiene usuarios, conversaciones, documentos,
  memoria, credenciales y trabajo operativo.
- El control de plataforma, que gestiona versiones, despliegues, salud técnica y
  mejoras depuradas.
- La red de conocimiento, que distribuye capacidades o información publicable
  sin abrir el acceso a las instancias de origen.

## 5. Ciclo operativo

Anacleto no espera siempre una petición. Puede iniciar trabajo ante eventos como
plazos, correos, cambios de expediente, reuniones, tareas bloqueadas o
solicitudes pendientes.

Su ciclo normal es:

1. Recibir una petición o detectar un evento autorizado.
2. Reunir el contexto permitido y verificar sus fuentes.
3. Identificar objetivo, responsable, competencias y restricciones.
4. Preparar un plan y evaluar el riesgo de cada acción.
5. Actuar, pedir aprobación o escalar según la política aplicable.
6. Coordinar sistemas y personas.
7. Registrar acciones, fuentes, delegación y resultados.
8. Supervisar el resultado y continuar cuando desaparezca un bloqueo.
9. Notificar según importancia, sin generar ruido innecesario.

## 6. Autonomía y autoridad

La autonomía se gobierna en capas:

1. Política general del producto, con garantías legales, de aislamiento,
   trazabilidad y seguridad que un ayuntamiento no puede rebajar.
2. Política de cada ayuntamiento, adaptada a su organización y sistemas.
3. Competencias y responsabilidades asociadas al cargo o función.
4. Delegaciones concretas del usuario, limitadas por las capas anteriores.

Cada acción se evalúa por datos afectados, destinatarios, impacto jurídico,
económico o laboral, reversibilidad, límites temporales o presupuestarios y
necesidad de informar.

Los niveles de autonomía son:

- **Observar:** consultar, analizar y detectar.
- **Proponer:** preparar una actuación sin ejecutarla.
- **Actuar y notificar:** ejecutar acciones reversibles y dejar constancia.
- **Actuar por delegación:** trabajar de forma independiente dentro de un
  mandato definido.
- **Aprobación obligatoria:** detenerse antes de acciones sensibles,
  irreversibles o reservadas a una persona.

La confirmación no se aplicará por igual a todas las escrituras. La política de
riesgo decidirá cuándo Anacleto puede actuar, cuándo debe avisar y cuándo necesita
una aprobación previa.

## 7. Identidad, colaboración y conflictos

Anacleto tendrá identidad técnica propia. Una acción mostrará quién la delegó,
qué permisos se utilizaron y quién la confirmó cuando fuera necesario. Una
comunicación externa o interna debe identificarse como enviada por Anacleto por
delegación de una persona; nunca debe suplantarla silenciosamente.

Los trabajadores también conversan directamente con Anacleto. Este puede hacer
preguntas, asignar trabajo, solicitar información, coordinar participantes y
hacer seguimiento dentro de sus márgenes de autonomía.

Ante instrucciones incompatibles no prevalece automáticamente la jerarquía ni
la instrucción más reciente. Anacleto aplica competencias legales,
responsabilidad sobre el asunto y políticas vigentes. Si el conflicto continúa,
detiene solo la actuación afectada, explica el bloqueo y lo escala.

No fuerza consenso. Conserva las posiciones relevantes, identifica quién tiene
competencia para decidir y permite reabrir la decisión cuando aparezca nueva
información.

## 8. Memoria y ciclo de vida de la información

Anacleto clasifica la información en ámbitos como personal, departamental, de
expediente y municipal. El acceso no depende de que toda una conversación sea
pública o privada, sino del ámbito, finalidad y permisos de cada dato o artefacto.

Puede clasificar automáticamente cuando el destino es evidente y de bajo riesgo.
Debe pedir permiso cuando:

- La clasificación sea ambigua.
- Una información privada vaya a incorporarse a memoria organizacional.
- Aumente la audiencia o cruce departamentos.
- Contenga datos personales, laborales, jurídicos o especialmente sensibles.
- Vaya a asociarse a un expediente oficial.
- Pueda producir una actuación o comunicación externa.

El usuario podrá consultar y corregir lo recordado. La eliminación estará sujeta
a las obligaciones de conservación aplicables.

La retención se define por clase de información: conversación transitoria,
memoria personal, conocimiento departamental u organizacional, documento oficial
y registro de auditoría. No se conserva todo indefinidamente ni se aplica un
único plazo universal.

## 9. Espacio propio y sistemas oficiales

El modelo es híbrido:

- Anacleto mantiene conversación, memoria, planes, tareas y automatizaciones.
- Los gestores de expedientes, registro, contabilidad y otros sistemas existentes
  continúan siendo la fuente oficial mientras no sean sustituidos expresamente.
- Cada dato debe indicar su fuente, vigencia y autoridad.
- La aplicación puede asumir progresivamente nuevas funciones sin exigir una
  sustitución completa inicial.

Las integraciones usan API oficial cuando exista. Si no existe, puede emplearse
automatización controlada de interfaces, con permisos, evidencias y controles
adicionales ante operaciones sensibles o interfaces inestables. Las credenciales
permanecen dentro de la instancia municipal.

## 10. Modelos de IA y salida de datos

La primera etapa utilizará proveedores externos. Un proveedor aprobado
contractualmente podrá procesar todos los datos necesarios para ejecutar la
tarea. Esto no autoriza a enviar contexto irrelevante: se mantiene la
minimización y se registra proveedor, modelo, finalidad y datos o categorías
enviadas.

El gateway es una abstracción propia y el único punto de política para llamadas
LLM. Debe permitir cambiar de proveedor sin reescribir Anacleto. Más adelante se
incorporarán modelos propios detrás del mismo contrato y se migrarán tareas de
forma gradual.

Un proveedor no aprobado queda bloqueado para datos municipales. La aprobación
debe cubrir, entre otros aspectos, confidencialidad, entrenamiento, retención,
seguridad, subencargados y ubicación del tratamiento.

## 11. Fiabilidad, prioridad y atención

Anacleto distingue hechos, inferencias, propuestas e incertidumbre. Las materias
jurídicas, económicas y las actuaciones oficiales exigen fuentes vigentes,
trazabilidad y el nivel de validación correspondiente. Cuando la evidencia sea
insuficiente, investiga, pregunta o detiene la actuación.

La prioridad del trabajo combina plazos legales, urgencia, impacto, dependencias,
responsabilidad, objetivos municipales, carga y disponibilidad. Los objetivos
políticos u organizativos no desplazan obligaciones legales o controles
internos.

Las notificaciones se ajustan a la relevancia:

- Inmediatas para decisiones, riesgos, conflictos y bloqueos.
- Resúmenes para actividad rutinaria.
- Silencio operativo para actuaciones normales dentro de una delegación.
- Historial completo disponible para auditoría.

## 12. Adaptación de cada instancia

La incorporación de un ayuntamiento es guiada. Configura estructura, cargos,
competencias, permisos, calendarios, políticas, procedimientos, fuentes e
integraciones. Después Anacleto aprende continuamente y propone cambios.

Los cambios de permisos, competencias o políticas siempre requieren validación y
versionado. La adaptación no puede convertir deducciones del modelo en autoridad
organizativa sin revisión.

## 13. Evolución del producto mediante el uso

El sistema de requisitos es una capacidad de descubrimiento y evolución, no el
propósito principal de Anacleto.

Cuando detecta una carencia, Anacleto primero comprueba si puede resolverla con
una función, configuración o procedimiento existente. Después dialoga y debate
con el usuario para comprender el problema, resultado esperado, contexto,
excepciones, alternativas e impacto. Resume lo entendido y permite corregirlo
antes de generar una propuesta estructurada.

Si la necesidad afecta a varias áreas, puede entrevistar o reunir a las personas
implicadas. La propuesta conserva desacuerdos y señala quién tiene competencia
para decidir. El usuario valida que la formulación representa la necesidad antes
de enviarla.

Los errores técnicos y aprendizajes anonimizados se envían automáticamente al
sistema central. Si la instancia no puede garantizar la depuración, bloquea el
envío. El desarrollador revisa todo lo recibido antes de convertirlo en una
mejora versionada y distribuible. Cada ayuntamiento puede consultar el registro
de lo compartido.

## 14. Administración de plataforma

El desarrollador dispone de control central sobre infraestructura, versiones,
despliegues y diagnósticos depurados. Esto no implica acceso permanente al
contenido municipal.

El acceso de soporte a contenido utiliza una identidad específica, motivo y
duración limitados, auditoría y capacidad de revocación. Las intervenciones
ordinarias pueden estar preautorizadas contractualmente; los datos especialmente
sensibles pueden exigir controles adicionales.

## 15. Criterios de éxito

El éxito no se mide por el número de mensajes ni por la cantidad de módulos. Se
evaluará mediante:

- Trabajo completado correctamente de extremo a extremo.
- Tiempo y pasos manuales evitados.
- Cumplimiento de plazos y reducción de bloqueos.
- Porcentaje de actuaciones resueltas dentro de una delegación.
- Correcciones, reversiones y escalados necesarios.
- Calidad y vigencia de las fuentes utilizadas.
- Incidentes de permisos, privacidad o seguridad.
- Confianza, adopción y capacidad de supervisión de los usuarios.

## 16. No objetivos

- Construir un chatbot que solo responda preguntas.
- Convertir cada capacidad municipal en un producto o asistente independiente.
- Compartir una base de datos operativa entre ayuntamientos.
- Dar al desarrollador acceso indiscriminado y permanente al contenido.
- Ejecutar silenciosamente acciones fuera de competencias o delegaciones.
- Mezclar actividad institucional con actividad partidista o electoral.
- Exponer el Anacleto interno directamente a ciudadanos.
- Priorizar ahora el alojamiento de modelos propios frente al núcleo operativo.
