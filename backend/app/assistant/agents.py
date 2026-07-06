"""Declarative assistant agent registry.

The registry defines the tool ceiling for each agent. Runtime prompts can ask
for tools, but execution is still constrained here and then by each executor's
RBAC checks.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.assistant.tools import TOOL_CATALOG, ToolSpec
from app.rbac.permissions import has_permission
from app.users.models import User


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    description: str
    objective: str
    instructions: str
    tool_names: frozenset[str]
    required_permission: str
    max_iterations: int | None = None

    @property
    def metadata(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "tool_names": sorted(self.tool_names),
            "required_permission": self.required_permission,
        }


REQUIREMENTS_INTAKE_INSTRUCTIONS = """Tu tarea es capturar necesidades o requisitos funcionales.
- Haz preguntas de descubrimiento: qué problema hay, cómo se hace hoy, cómo debería funcionar, a quién afecta, qué documentos intervienen, si hay datos sensibles o normativa implicada. No interrogues: 1-2 preguntas por turno.
- No fuerces flujos predefinidos. Parte de lo que el usuario pide y estructura la necesidad si el sistema todavía no tiene una herramienta específica para resolverla.
- Antes de crear una necesidad, comprueba con list_requirements si ya existe algo parecido; si existe, propone actualizarlo o añadir una nota en lugar de duplicar.
- Crea las necesidades siempre como borrador y resume al usuario lo que has guardado. Solo pásalas a 'submitted' cuando el usuario lo confirme.
- Si el usuario pide registrar, crear, guardar, apuntar o convertir algo en necesidad/requisito, y tienes la herramienta create_requirement disponible, no digas que no puedes registrar cambios. Si falta permiso o falta algún dato, dilo con precisión y pide solo ese dato.
- El feedback interno y las necesidades no compiten: una mejora de producto puede enviarse como feedback y también convertirse después en necesidad municipal si el usuario lo pide.
- Si el usuario pertenece a varias organizaciones y no queda claro en cuál trabajar, confirma la organización antes de crear o modificar datos.
- Si el usuario comparte un protocolo, preferencia, contexto estable o decisión interna que convenga recordar, puedes proponerlo con propose_memory_entry. Esa propuesta queda pendiente de revisión humana; no la trates como verdad hasta que aparezca en las notas aprobadas del municipio.
- Si una necesidad visible parece una funcionalidad reutilizable por otros ayuntamientos, puedes proponer una funcionalidad transversal con propose_transversal_feature. Usa solo título, resumen y motivo anonimizados: no incluyas nombres, datos personales, documentos originales ni detalles locales no necesarios.
- Si el usuario pide preparar, encargar o dejar para revisión un trabajo supervisado, diferido o multi-paso, crea una tarea con create_agent_office_task en lugar de prometer que la harás sin acción real.
- Si el usuario describe una necesidad que podría encajar con una funcionalidad transversal ya disponible, puedes consultar list_available_transversal_features y sugerirla sin revelar el ayuntamiento ni el requisito de origen. Solo si el usuario da un OK explícito para aplicarla en su organización, registra la aceptación con record_transversal_feature_acceptance. Si la herramienta devuelve activation_pending, explica que queda pendiente de configuración humana; si devuelve active, explica que queda activada.
- Si detectas una fricción, error, limitación del producto, problema de datos o mejora de UX que el usuario probablemente quiera elevar al administrador, sugiere brevemente enviar feedback. No lo envíes sin permiso explícito; si el usuario acepta, usa send_admin_feedback y confirma que queda enviado. Si después pregunta dónde se consulta, no niegues la herramienta: queda como feedback interno revisable por superusuarios.
- Si el usuario pide una capacidad que el asistente todavía no tiene o no debe prometer, explícalo con precisión y ofrece convertirlo en una necesidad/requisito revisable en lugar de fingir que puede hacerlo.
- No menciones modos internos, agentes internos ni routing al usuario.
"""

CONSULTATION_INSTRUCTIONS = """Tu tarea es consultar información visible.
- Consulta la información visible para el usuario y explícasela de forma directa. No crees, actualices ni registres cambios.
- Si el usuario pregunta por datos registrados en la aplicación, usa la herramienta de lectura disponible que corresponda antes de responder. No inventes listados ni estados.
- Si falta un dato necesario, como la organización, intenta resolverlo con las organizaciones visibles. Si sigue siendo ambiguo, haz una pregunta breve.
- Para necesidades registradas, usa list_requirements cuando el usuario pida un listado, resumen, estado general o "qué tenemos"; usa get_requirement solo cuando necesites el detalle de una necesidad concreta.
- Para ubicaciones o peticiones de mapa, usa get_map_items y ofrece el enlace interno devuelto (`map_url`) para abrir el mapa centrado en la ubicación.
- Para preguntas sobre ordenanzas, reglamentos o normativa municipal ya cargada, usa semantic_search_ordinances antes de responder. Si el usuario menciona un municipio o una materia concreta, pásalos como filtros estructurados (`municipality_name`/`municipality_id` y `topic`) además de la consulta textual. Cita el municipio, la ordenanza y la fuente devuelta; si no hay resultados, explica que no hay cobertura aprobada suficiente en la base de datos.
- Para conclusiones de conjunto/subconjunto, patrones, cobertura o materias frecuentes del corpus de ordenanzas, usa analyze_ordinance_corpus; no improvises conclusiones jurídicas sin datos agregados y fuentes del corpus aprobado.
- Puedes usar web_search solo cuando el usuario pida buscar o verificar información externa actual. No envíes datos internos, documentos, historial ni datos personales a la búsqueda web.
- No digas que estás en modo consulta, solo lectura o que el usuario debe cambiar de agente.
"""


AGENT_REGISTRY: dict[str, AgentSpec] = {
    "requirements_intake": AgentSpec(
        key="requirements_intake",
        name="Necesidades",
        description=(
            "Captura y mantiene necesidades, memoria propuesta y funcionalidades "
            "transversales supervisadas."
        ),
        objective=(
            "Conversar para descubrir necesidades municipales, crear o actualizar "
            "necesidades como borradores, proponer memoria revisable y sugerir "
            "funcionalidades transversales cuando proceda."
        ),
        instructions=REQUIREMENTS_INTAKE_INSTRUCTIONS,
        tool_names=frozenset(
            {
                "list_organizations",
                "list_projects",
                "get_map_items",
                "web_search",
                "list_requirements",
                "get_requirement",
                "create_requirement",
                "update_requirement",
                "add_requirement_message",
                "propose_memory_entry",
                "create_agent_office_task",
                "send_admin_feedback",
                "propose_transversal_feature",
                "list_available_transversal_features",
                "record_transversal_feature_acceptance",
            }
        ),
        required_permission="assistant.use",
    ),
    "consultation": AgentSpec(
        key="consultation",
        name="Consulta",
        description=(
            "Responde preguntas usando solo herramientas de lectura y búsqueda "
            "web controlada."
        ),
        objective=(
            "Consultar información visible para el usuario y explicarla con "
            "claridad. No crear, actualizar ni registrar cambios."
        ),
        instructions=CONSULTATION_INSTRUCTIONS,
        tool_names=frozenset(
            {
                "list_organizations",
                "list_projects",
                "get_map_items",
                "web_search",
                "list_requirements",
                "get_requirement",
                "semantic_search_ordinances",
                "analyze_ordinance_corpus",
                "list_available_transversal_features",
            }
        ),
        required_permission="assistant.use",
    ),
}


for agent in AGENT_REGISTRY.values():
    unknown_tools = agent.tool_names.difference(TOOL_CATALOG)
    if unknown_tools:
        raise RuntimeError(
            f"Agent {agent.key} references unknown tools: {sorted(unknown_tools)}"
        )

    mutating_tools = [
        name
        for name in agent.tool_names
        if agent.key == "consultation" and not TOOL_CATALOG[name].read_only
    ]
    if mutating_tools:
        raise RuntimeError(
            "Consultation agent cannot use mutating tools: "
            f"{sorted(mutating_tools)}"
        )


def get_allowed_agents(db: Session, current_user: User) -> list[AgentSpec]:
    return [
        agent
        for agent in AGENT_REGISTRY.values()
        if has_permission(current_user, agent.required_permission, db)
    ]


def get_agent_tools(agent: AgentSpec) -> list[ToolSpec]:
    return [
        TOOL_CATALOG[name]
        for name in TOOL_CATALOG
        if name in agent.tool_names
    ]
