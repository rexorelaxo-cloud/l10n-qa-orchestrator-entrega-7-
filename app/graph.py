"""Grafo principal del L10n QA Orchestrator (LangGraph ``StateGraph``).

Topología jerárquica con Supervisor:

    START → supervisor ─┬→ research ──┐
                        ├→ analysis ──┼→ supervisor (o validation si queda un solo paso)
                        ├→ validation ─┬→ supervisor (si necesita refinamiento)
                        └→ END         └→ llm_report → approval_gate ─(interrupt)→ execute_import → END

Las rutas surgen de las decisiones del Supervisor (aristas condicionales), no de una secuencia fija.
"""
from pathlib import Path
from typing import Callable, Literal, Optional

from langgraph.graph import END, START, StateGraph

from app.agents.report_agent import crear_nodo_redactor
from app.config import Settings
from app.hitl import crear_nodo_importacion, enrutar_tras_aprobacion, nodo_puerta_aprobacion
from app.llm.factory import ManejadorLLM
from app.observability import medir_nodo

from app.agents.analyst_agent import analysis_node
from app.agents.research_agent import research_node
from app.agents.supervisor_agent import enrutar_supervisor, supervisor_node
from app.agents.validation_agent import enrutar_validacion, validation_node
from app.state import L10nState


def enrutar_tras_especialista(state: L10nState) -> Literal["supervisor", "validation"]:
    """Los especialistas devuelven el control al Supervisor.

    Excepción de seguridad: si solo queda una ejecución disponible, se pasa directamente a la
    validación para cerrar de forma controlada sin superar ``max_steps``.
    """
    if state["step_count"] >= state["max_steps"] - 1:
        return "validation"
    return "supervisor"


def construir_grafo(
    research: Optional[Callable] = None,
    analysis: Optional[Callable] = None,
    supervisor: Optional[Callable] = None,
    validation: Optional[Callable] = None,
    *,
    checkpointer=None,
    llm: Optional[ManejadorLLM] = None,
    settings: Optional[Settings] = None,
    redactor: Optional[Callable] = None,
    puerta: Optional[Callable] = None,
    importacion: Optional[Callable] = None,
):
    """Construye y compila el grafo. Los parámetros permiten sustituir nodos (útil en pruebas).

    Módulo 7: tras la validación se agregan ``llm_report`` (resumen redactado), ``approval_gate``
    (interrupción humana) y ``execute_import`` (operación crítica simulada). ``checkpointer`` es el
    guardado persistente (Redis) que permite reanudar el mismo ``thread_id``.
    """
    settings = settings or Settings.from_env()
    if redactor is None:
        redactor = crear_nodo_redactor(llm or ManejadorLLM("local", "ninguno (determinista)", False), settings)
    g = StateGraph(L10nState)
    g.add_node("supervisor", medir_nodo("supervisor", supervisor or supervisor_node))
    g.add_node("research", medir_nodo("research", research or research_node))
    g.add_node("analysis", medir_nodo("analysis", analysis or analysis_node))
    g.add_node("validation", medir_nodo("validation", validation or validation_node))
    g.add_node("llm_report", medir_nodo("llm_report", redactor))
    g.add_node("approval_gate", puerta or nodo_puerta_aprobacion)
    g.add_node("execute_import", medir_nodo("execute_import", importacion or crear_nodo_importacion(settings.output_dir)))

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor", enrutar_supervisor,
        {"research": "research", "analysis": "analysis", "validation": "validation", "END": END},
    )
    g.add_conditional_edges("research", enrutar_tras_especialista,
                            {"supervisor": "supervisor", "validation": "validation"})
    g.add_conditional_edges("analysis", enrutar_tras_especialista,
                            {"supervisor": "supervisor", "validation": "validation"})
    g.add_conditional_edges("validation", enrutar_validacion, {"supervisor": "supervisor", "END": "llm_report"})
    g.add_edge("llm_report", "approval_gate")
    g.add_conditional_edges("approval_gate", enrutar_tras_aprobacion, {"execute_import": "execute_import", "END": END})
    g.add_edge("execute_import", END)
    return g.compile(checkpointer=checkpointer)


graph = construir_grafo()


def obtener_mermaid(grafo=None) -> str:
    """Diagrama Mermaid del grafo compilado (no requiere dependencias opcionales)."""
    return (grafo or graph).get_graph().draw_mermaid()


def guardar_diagrama_png(ruta: str, grafo=None) -> bool:
    """Intenta generar un PNG del grafo con LangGraph (requiere red o dependencias opcionales).

    Devuelve ``True`` si se generó. Si falla, el diagrama Mermaid sigue disponible.
    """
    try:
        png = (grafo or graph).get_graph().draw_mermaid_png()
        with open(ruta, "wb") as f:
            f.write(png)
        return True
    except Exception:
        return False
