"""Supervisor: controlador central del grafo.

Decide, a partir del estado compartido, qué agente interviene, si hace falta un refinamiento y
cuándo se puede pasar a la validación final. Nunca ejecuta el trabajo de los especialistas.
"""
from typing import Optional

from langchain_core.messages import AIMessage

from app.state import (
    L10nState, RutaSupervisor, categorias_cubiertas, contribuciones_de, controles_solicitados,
    crear_contribucion, datos_analisis, entradas_investigacion, primera_inspeccion,
)
from app.tools.l10n_rules import CONTROLES, categorias_requeridas


def _ya_pedido(state: dict, clave: str) -> bool:
    return any(r.get("clave") == clave for r in state.get("refinement_log", []))


def _clave_refinamiento(destino: str, tipo: str, elementos: list[str]) -> str:
    return f"{destino}:{tipo}:{'|'.join(sorted(elementos))}"


def decidir_siguiente(state: dict) -> dict:
    """Lógica de decisión pura (sin efectos): devuelve ruta, motivo y datos opcionales.

    Orden de criterios:
      1. Finalización ya marcada -> END.
      2. Reserva del último paso para la validación (límite de ejecución).
      3. Entradas inválidas (solicitud vacía) -> validación (fallo controlado).
      4. Refinamiento pendiente pedido por la validación -> su especialista.
      5. Falta investigación -> research. Sin archivos -> validación (fallo controlado).
      6. Cobertura de reglas incompleta (una sola vez) -> refinamiento a research.
      7. Falta análisis -> analysis. Cobertura de controles incompleta (una sola vez) -> refinamiento.
      8. Todo suficiente -> validation.
    """
    max_pasos = state.get("max_steps", 12)
    n = state.get("step_count", 0) + 1  # ejecución actual del supervisor

    if state.get("task_completed"):
        return {"ruta": "END", "motivo": "La tarea ya fue marcada como completada."}

    if n >= max_pasos - 1:
        return {"ruta": "validation",
                "motivo": f"Límite de ejecución próximo ({n}/{max_pasos}): se reserva el último paso "
                          "para la validación y el cierre controlado."}

    if not state.get("user_request", "").strip():
        return {"ruta": "validation", "motivo": "La solicitud está vacía: fallo controlado de entrada."}

    pendiente = state.get("pending_refinement")
    if pendiente:
        return {"ruta": pendiente["destino"], "refinamiento": pendiente,
                "motivo": f"Refinamiento pendiente para {pendiente['destino']}: {pendiente['motivo']}"}

    if not entradas_investigacion(state, "inspeccion"):
        return {"ruta": "research", "motivo": "Falta evidencia: aún no se inspeccionaron los archivos ni se consultaron reglas."}

    inspeccion = primera_inspeccion(state)
    if not inspeccion["archivos"]:
        return {"ruta": "validation", "motivo": "No se encontraron archivos para analizar: fallo controlado de entrada.",
                "incertidumbres": ["No se recibió ningún archivo de entrada."]}

    solicitados = controles_solicitados(state)
    requeridas = categorias_requeridas(solicitados)
    faltantes = [c for c in requeridas if c not in categorias_cubiertas(state)]
    if faltantes:
        clave = _clave_refinamiento("research", "buscar_reglas", faltantes)
        if not _ya_pedido(state, clave):
            return {"ruta": "research", "motivo": f"Las reglas recuperadas no cubren las categorías: {', '.join(faltantes)}.",
                    "refinamiento": {"destino": "research", "tipo": "buscar_reglas", "origen": "supervisor",
                                     "motivo": f"Faltan reglas de las categorías: {', '.join(faltantes)}.",
                                     "detalle": {"categorias": faltantes}, "clave": clave}}

    if not contribuciones_de(state, "analysis"):
        return {"ruta": "analysis", "motivo": "La investigación es suficiente y falta el análisis estructural.",
                "incertidumbres": [f"Sin reglas recuperadas para la categoría «{c}»: no se ejecutan los controles asociados."
                                   for c in faltantes] if faltantes else None}

    cubiertas = categorias_cubiertas(state)
    ejecutados = datos_analisis(state)["controles_ejecutados"]
    por_ejecutar = [c for c in solicitados if c not in ejecutados and CONTROLES[c][0] in cubiertas]
    if por_ejecutar:
        clave = _clave_refinamiento("analysis", "ejecutar_controles", por_ejecutar)
        if not _ya_pedido(state, clave):
            return {"ruta": "analysis", "motivo": f"El análisis no cubre los controles solicitados: {', '.join(por_ejecutar)}.",
                    "refinamiento": {"destino": "analysis", "tipo": "ejecutar_controles", "origen": "supervisor",
                                     "motivo": f"Faltan controles solicitados: {', '.join(por_ejecutar)}.",
                                     "detalle": {"controles": por_ejecutar}, "clave": clave}}

    return {"ruta": "validation", "motivo": "Investigación y análisis suficientes: se pasa a la validación final."}


def supervisor_node(state: L10nState) -> dict:
    """Nodo de LangGraph del Supervisor."""
    decision = decidir_siguiente(state)
    n = state.get("step_count", 0) + 1
    ruta = decision["ruta"]
    refin = decision.get("refinamiento")
    nuevo_refin = refin if refin and not state.get("pending_refinement") else None

    contribucion = crear_contribucion(
        paso=n, agente="supervisor", tipo_analisis="decision_de_enrutamiento",
        resumen=f"Ruta elegida: {ruta}. {decision['motivo']}",
        solicitud_refinamiento=refin,
        datos={"ruta": ruta, "motivo": decision["motivo"]},
    )
    actualizacion: dict = {
        "next_agent": ruta,
        "step_count": 1,
        "routing_log": [{"paso": n, "ruta": ruta, "motivo": decision["motivo"], "refinamiento": bool(refin)}],
        "agent_contributions": [contribucion],
        "messages": [AIMessage(content=f"[Supervisor] → {ruta}. {decision['motivo']}", name="supervisor")],
    }
    if nuevo_refin:
        actualizacion["pending_refinement"] = nuevo_refin
        actualizacion["refinement_log"] = [nuevo_refin]
    if decision.get("incertidumbres"):
        actualizacion["uncertainties"] = [u for u in decision["incertidumbres"] if u]
    return actualizacion


def enrutar_supervisor(state: L10nState) -> RutaSupervisor:
    """Función de arista condicional: lee la decisión que guardó el Supervisor en el estado."""
    return state["next_agent"]  # type: ignore[return-value]
