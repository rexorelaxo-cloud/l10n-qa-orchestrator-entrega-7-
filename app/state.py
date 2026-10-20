"""Estado compartido del L10n QA Orchestrator.

El estado hereda de ``MessagesState`` (LangGraph), por lo que conserva el campo
``messages`` con su reducer ``add_messages``, y agrega los campos propios de la tarea.

Semántica de actualización de cada campo
----------------------------------------
REEMPLAZAN el valor anterior (último valor gana):
    user_request, source_files, target_files, validation_results, next_agent,
    task_completed, max_steps, final_report, pending_refinement, config.

ACUMULAN (reducer ``operator.add``; los nodos devuelven solo lo NUEVO):
    messages (``add_messages``), research_findings, analysis_findings,
    agent_contributions, errors, uncertainties, conflicts, refinement_log,
    routing_log y step_count (cada nodo devuelve ``1``).

Esto evita que un nodo sobrescriba accidentalmente resultados anteriores y permite
reconstruir la historia completa de contribuciones.
"""
import operator
from typing import Annotated, Any, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import MessagesState

# --- Valores estables (identificadores internos) -----------------------------------
Severidad = Literal["error", "warning", "info"]
EstadoHallazgo = Literal["confirmed", "rejected", "pending"]
EstadoGeneral = Literal["PASS", "FAIL", "NEEDS_REVIEW", "INCOMPLETE"]
RutaSupervisor = Literal["research", "analysis", "validation", "END"]

MAX_PASOS_POR_DEFECTO = 12
MIN_PASOS_PERMITIDO = 4

# Equivalentes en español para los informes dirigidos a personas.
ESTADO_GENERAL_ES = {
    "PASS": "APROBADO",
    "FAIL": "FALLIDO",
    "NEEDS_REVIEW": "REQUIERE REVISIÓN",
    "INCOMPLETE": "INCOMPLETO",
}
SEVERIDAD_ES = {"error": "error", "warning": "advertencia", "info": "información"}
ESTADO_HALLAZGO_ES = {"confirmed": "confirmado", "rejected": "rechazado", "pending": "pendiente"}


class Hallazgo(TypedDict, total=False):
    """Hallazgo estructurado producido por el Agente de Análisis."""

    id: str
    severidad: Severidad
    categoria: str
    regla: str
    archivo: str          # archivo de destino analizado
    archivo_origen: str
    idioma: Optional[str]
    ruta_json: str        # p. ej. "page.banner.heading" o "links[1].label"
    mensaje: str
    evidencia: dict
    estado: EstadoHallazgo
    excepcion_aplicada: Optional[dict]
    agente: str


class Contribucion(TypedDict, total=False):
    """Registro trazable de lo que aportó un agente en un paso."""

    paso: int
    agente: str
    tipo_analisis: str
    hallazgos: list            # identificadores de los hallazgos producidos
    evidencias: list           # herramientas / archivos / reglas utilizados
    estado_hallazgos: dict     # {"confirmed": n, "rejected": n, "pending": n}
    solicitud_refinamiento: Optional[dict]
    atendio_refinamiento: Optional[dict]
    resumen: str
    datos: dict


class SolicitudRefinamiento(TypedDict, total=False):
    """Pedido concreto del Supervisor o de la Validación a un especialista."""

    destino: Literal["research", "analysis"]
    tipo: str                  # buscar_reglas | verificar_archivos | ejecutar_controles
    motivo: str
    detalle: dict
    origen: str                # quién lo pidió: supervisor | validation
    clave: str                 # clave estable para no repetir refinamientos


class L10nState(MessagesState):
    """Estado compartido entre todos los nodos del grafo."""

    user_request: str
    source_files: list[str]
    target_files: list[str]
    research_findings: Annotated[list[dict], operator.add]
    analysis_findings: Annotated[list[Hallazgo], operator.add]
    validation_results: dict
    agent_contributions: Annotated[list[Contribucion], operator.add]
    next_agent: str
    task_completed: bool
    step_count: Annotated[int, operator.add]
    max_steps: int
    errors: Annotated[list[str], operator.add]
    final_report: dict
    # --- Campos auxiliares ---
    pending_refinement: Optional[SolicitudRefinamiento]
    refinement_log: Annotated[list[dict], operator.add]
    routing_log: Annotated[list[dict], operator.add]
    uncertainties: Annotated[list[str], operator.add]
    conflicts: Annotated[list[dict], operator.add]
    config: dict
    # --- Campos del Módulo 7 (API, LLM y aprobación humana) ---
    job_id: str
    thread_id: str
    require_approval: bool
    stage_timings: Annotated[list[dict], operator.add]
    resumen_llm: dict
    import_status: str          # NO_SOLICITADA | NO_ELEGIBLE | ESPERANDO_APROBACION | APROBADA | RECHAZADA | EJECUTADA
    approval: dict              # decisión humana registrada (decisión, aprobador, comentario, fecha)
    import_result: dict


def crear_estado_inicial(
    solicitud: str,
    archivos_origen: list[str],
    archivos_destino: list[str],
    max_pasos: int = MAX_PASOS_POR_DEFECTO,
    config: Optional[dict] = None,
    job_id: str = "",
    thread_id: str = "",
    require_approval: bool = False,
) -> dict:
    """Construye el estado inicial completo de una ejecución."""
    from app.tools.l10n_rules import CONFIG_POR_DEFECTO

    if max_pasos < MIN_PASOS_PERMITIDO:
        raise ValueError(
            f"max_pasos debe ser al menos {MIN_PASOS_PERMITIDO} para permitir una "
            "ejecución mínima (supervisor, especialista, supervisor, validación)."
        )
    return {
        "messages": [HumanMessage(content=solicitud or "(solicitud vacía)", name="usuario")],
        "user_request": solicitud or "",
        "source_files": list(archivos_origen),
        "target_files": list(archivos_destino),
        "research_findings": [],
        "analysis_findings": [],
        "validation_results": {},
        "agent_contributions": [],
        "next_agent": "",
        "task_completed": False,
        "step_count": 0,
        "max_steps": max_pasos,
        "errors": [],
        "final_report": {},
        "pending_refinement": None,
        "refinement_log": [],
        "routing_log": [],
        "uncertainties": [],
        "conflicts": [],
        "config": config if config is not None else CONFIG_POR_DEFECTO,
        "job_id": job_id,
        "thread_id": thread_id,
        "require_approval": require_approval,
        "stage_timings": [],
        "resumen_llm": {},
        "import_status": "NO_SOLICITADA",
        "approval": {},
        "import_result": {},
    }


# --- Utilidades de lectura del estado --------------------------------------------------
def crear_contribucion(
    paso: int,
    agente: str,
    tipo_analisis: str,
    resumen: str,
    hallazgos: Optional[list] = None,
    evidencias: Optional[list] = None,
    estado_hallazgos: Optional[dict] = None,
    solicitud_refinamiento: Optional[dict] = None,
    atendio_refinamiento: Optional[dict] = None,
    datos: Optional[dict] = None,
) -> Contribucion:
    return {
        "paso": paso,
        "agente": agente,
        "tipo_analisis": tipo_analisis,
        "hallazgos": hallazgos or [],
        "evidencias": evidencias or [],
        "estado_hallazgos": estado_hallazgos or {"confirmed": 0, "rejected": 0, "pending": 0},
        "solicitud_refinamiento": solicitud_refinamiento,
        "atendio_refinamiento": atendio_refinamiento,
        "resumen": resumen,
        "datos": datos or {},
    }


def contar_por_estado(hallazgos: list) -> dict:
    cuenta = {"confirmed": 0, "rejected": 0, "pending": 0}
    for h in hallazgos:
        cuenta[h.get("estado", "pending")] = cuenta.get(h.get("estado", "pending"), 0) + 1
    return cuenta


def contribuciones_de(state: dict, agente: str) -> list[dict]:
    return [c for c in state.get("agent_contributions", []) if c.get("agente") == agente]


def entradas_investigacion(state: dict, tipo: str) -> list[dict]:
    return [e for e in state.get("research_findings", []) if e.get("tipo") == tipo]


def reglas_recuperadas(state: dict) -> dict[str, dict]:
    """Une todas las reglas recuperadas por el Agente de Investigación (por id)."""
    reglas: dict[str, dict] = {}
    for entrada in entradas_investigacion(state, "reglas"):
        for regla in entrada.get("datos", {}).get("reglas", []):
            reglas[regla["id"]] = regla
    return reglas


def categorias_cubiertas(state: dict) -> set[str]:
    return {r["categoria"] for r in reglas_recuperadas(state).values()}


def controles_solicitados(state: dict) -> list[str]:
    entradas = entradas_investigacion(state, "controles")
    return list(entradas[-1]["datos"]["controles_solicitados"]) if entradas else []


def primera_inspeccion(state: dict) -> Optional[dict]:
    entradas = entradas_investigacion(state, "inspeccion")
    return entradas[0]["datos"] if entradas else None


def estado_archivos_investigacion(state: dict) -> dict[str, dict]:
    """Último estado conocido por archivo según la investigación (inspección + reinspecciones)."""
    estado: dict[str, dict] = {}
    for entrada in state.get("research_findings", []):
        if entrada.get("tipo") in ("inspeccion", "reinspeccion"):
            for archivo in entrada.get("datos", {}).get("archivos", []):
                estado[archivo["ruta"]] = archivo
    return estado


def datos_analisis(state: dict) -> dict[str, Any]:
    """Consolida los datos de todas las contribuciones del Agente de Análisis."""
    consolidado: dict[str, Any] = {
        "controles_ejecutados": [],
        "controles_omitidos": [],
        "pares_comparados": [],
        "pares_sin_comparar": [],
        "no_analizables": [],
        "excepciones_aplicadas": [],
        "integridad": {},
    }
    for c in contribuciones_de(state, "analysis"):
        d = c.get("datos", {})
        for k in ("controles_ejecutados", "pares_comparados", "pares_sin_comparar",
                  "no_analizables", "excepciones_aplicadas"):
            for item in d.get(k, []):
                if item not in consolidado[k]:
                    consolidado[k].append(item)
        consolidado["controles_omitidos"] = d.get("controles_omitidos", consolidado["controles_omitidos"])
        for ruta, valores in d.get("integridad", {}).items():
            consolidado["integridad"].setdefault(ruta, valores)
            consolidado["integridad"][ruta]["sha256_despues"] = valores.get("sha256_despues")
    return consolidado
