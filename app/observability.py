"""Observabilidad: LangSmith (opcional), correlación por tarea, medición de etapas y uso de tokens.

* LangSmith se activa solo si ``LANGSMITH_TRACING=true`` Y hay ``LANGSMITH_API_KEY``. Sin credenciales la
  aplicación funciona igual, sin trazas externas, y lo informa en ``/health``.
* Cada ejecución del grafo se etiqueta con ``job_id`` y ``thread_id`` (``metadata`` y ``tags`` de la
  configuración de LangChain), de modo que las trazas pueden filtrarse por tarea.
* Los tiempos por etapa se miden siempre (con o sin LangSmith) y quedan en el estado y en Redis.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from app.config import Settings

log = logging.getLogger("l10n.observabilidad")

AGENTE_POR_NODO = {
    "supervisor": "Supervisor Agent", "research": "Research Agent", "analysis": "Analysis Agent",
    "validation": "Validation Node", "llm_report": "Report Agent (LLM)",
    "approval_gate": "Human-in-the-Loop", "execute_import": "Importación simulada",
}


def configurar_langsmith(settings: Settings) -> dict:
    """Aplica la configuración de LangSmith al entorno del proceso y devuelve su estado (sin secretos)."""
    if not settings.langsmith_tracing:
        os.environ["LANGSMITH_TRACING"] = "false"
        return {"activo": False, "motivo": "LANGSMITH_TRACING desactivado"}
    if not settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "false"
        log.warning("LANGSMITH_TRACING=true pero falta LANGSMITH_API_KEY: se ejecuta sin trazas externas.")
        return {"activo": False, "motivo": "falta LANGSMITH_API_KEY"}
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    return {"activo": True, "proyecto": settings.langsmith_project}


def config_ejecucion(job_id: str, thread_id: str, *, proveedor: str = "", modelo: str = "",
                     intento: int = 1, extra: Optional[dict] = None) -> dict:
    """``RunnableConfig`` de una ejecución: checkpoint por ``thread_id`` y metadatos de correlación."""
    metadata = {"job_id": job_id, "thread_id": thread_id, "llm_provider": proveedor,
                "llm_model": modelo, "intento": intento, **(extra or {})}
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": f"l10n-qa:{job_id}",
        "tags": ["l10n-qa-orchestrator", f"job:{job_id}"],
        "metadata": metadata,
        "recursion_limit": 50,
    }


def medir_nodo(nombre: str, fn: Callable) -> Callable:
    """Envuelve un nodo y agrega la duración de la etapa a ``stage_timings``."""
    def nodo(state, *args, **kwargs):
        t0 = time.perf_counter()
        resultado = fn(state, *args, **kwargs)
        dur = round(time.perf_counter() - t0, 6)
        resultado = dict(resultado or {})
        resultado["stage_timings"] = list(resultado.get("stage_timings", [])) + [
            {"nodo": nombre, "agente": AGENTE_POR_NODO.get(nombre, nombre),
             "segundos": dur, "paso": state.get("step_count", 0)}]
        return resultado
    nodo.__name__ = f"medido_{nombre}"
    return nodo


def extraer_uso(mensaje: Any) -> dict:
    """Tokens informados por el proveedor (``usage_metadata`` de LangChain). Vacío si no se informan."""
    uso = getattr(mensaje, "usage_metadata", None) or {}
    if not uso:
        return {"informado": False}
    return {"informado": True, "tokens_entrada": uso.get("input_tokens"),
            "tokens_salida": uso.get("output_tokens"), "tokens_totales": uso.get("total_tokens")}


def percentil(valores: list[float], p: float) -> Optional[float]:
    """Percentil por el método del rango más cercano (nearest-rank): ``ceil(p/100 · n)``-ésimo valor."""
    if not valores:
        return None
    import math
    ordenados = sorted(valores)
    k = max(1, math.ceil(p / 100 * len(ordenados)))
    return ordenados[k - 1]
