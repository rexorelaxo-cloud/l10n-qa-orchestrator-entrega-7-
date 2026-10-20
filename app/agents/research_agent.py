"""Agente de Investigación.

Responsabilidad: inspeccionar los archivos, identificar idiomas y pares, deducir qué controles
pide la solicitud, consultar la base de conocimiento local y determinar las excepciones
aplicables. NO ejecuta el análisis estructural ni modifica archivos.
"""
from langchain_core.messages import AIMessage

from app.state import (
    L10nState, crear_contribucion, entradas_investigacion, reglas_recuperadas,
)
from app.tools.file_inspector import inspect_localization_files
from app.tools.l10n_rules import (
    categorias_requeridas, excepciones_aplicables, inferir_controles, search_l10n_qa_rules,
)

INSTRUCCIONES_INVESTIGACION = (
    "Eres el Agente de Investigación de L10n QA. Inspeccioná los archivos, identificá idiomas y "
    "pares, recuperá las reglas de QA estructural aplicables y las excepciones configuradas. "
    "No ejecutes el análisis estructural, no modifiques archivos y citá siempre tus fuentes."
)

TOP_K_PRIMERA_CONSULTA = 5
TOP_K_REFINAMIENTO = 3


def contexto_investigacion(state: dict) -> dict:
    """Contexto MÍNIMO que recibe el Agente de Investigación (aislamiento de contexto).

    No incluye el historial de mensajes, los hallazgos de análisis ni las contribuciones de
    otros agentes.
    """
    refin = state.get("pending_refinement")
    return {
        "instrucciones": INSTRUCCIONES_INVESTIGACION,
        "solicitud": state.get("user_request", ""),
        "rutas_origen": list(state.get("source_files", [])),
        "rutas_destino": list(state.get("target_files", [])),
        "config": state.get("config", {}),
        "refinamiento": refin if refin and refin.get("destino") == "research" else None,
        "ya_inspeccionado": bool(entradas_investigacion(state, "inspeccion")),
        "paso": state.get("step_count", 0) + 1,
        "siguiente_id": len(state.get("research_findings", [])) + 1,
    }


def _id(n: int) -> str:
    return f"R-{n:03d}"


def _resumen_reglas(reglas: list[dict]) -> list[dict]:
    return [{"id": r["id"], "categoria": r["categoria"], "titulo": r["titulo"],
             "descripcion": r["descripcion"], "severidad_por_defecto": r["severidad_por_defecto"],
             "puntaje": r.get("puntaje", 0)} for r in reglas]


def _investigacion_inicial(ctx: dict) -> tuple[list[dict], list[str], str]:
    """Primera pasada: inspección + deducción de controles + consulta de reglas + excepciones."""
    n = ctx["siguiente_id"]
    inspeccion = inspect_localization_files.invoke(
        {"source_paths": ctx["rutas_origen"], "target_paths": ctx["rutas_destino"]})
    controles = inferir_controles(ctx["solicitud"])
    categorias = categorias_requeridas(controles)
    reglas = search_l10n_qa_rules.invoke({"query": ctx["solicitud"], "top_k": TOP_K_PRIMERA_CONSULTA})

    nombres: set[str] = set()
    for a in inspeccion["archivos"]:
        if a["rol"] == "origen":
            nombres |= set(a["nombres_claves"])
    excepciones = excepciones_aplicables(ctx["config"], nombres)

    entradas = [
        {"id": _id(n), "tipo": "inspeccion", "status": "confirmed",
         "descripcion": f"{len(inspeccion['archivos'])} archivo(s), {len(inspeccion['pares'])} par(es) origen/destino.",
         "evidencia": {"herramienta": "inspect_localization_files",
                       "huellas_sha256": {a["ruta"]: a["sha256"] for a in inspeccion["archivos"]}},
         "datos": inspeccion},
        {"id": _id(n + 1), "tipo": "controles", "status": "confirmed",
         "descripcion": "Controles estructurales deducidos de la solicitud.",
         "evidencia": {"herramienta": "inferir_controles", "solicitud": ctx["solicitud"]},
         "datos": {"controles_solicitados": controles, "categorias_requeridas": categorias}},
        {"id": _id(n + 2), "tipo": "reglas", "status": "confirmed",
         "descripcion": f"{len(reglas)} regla(s) recuperadas de la base de conocimiento.",
         "evidencia": {"herramienta": "search_l10n_qa_rules", "consulta": ctx["solicitud"],
                       "top_k": TOP_K_PRIMERA_CONSULTA},
         "datos": {"reglas": _resumen_reglas(reglas)}},
        {"id": _id(n + 3), "tipo": "excepciones", "status": "confirmed",
         "descripcion": f"{len(excepciones)} excepción(es) configurada(s) aplicable(s).",
         "evidencia": {"herramienta": "excepciones_aplicables",
                       "claves_presentes_en_origen": [e["clave"] for e in excepciones]},
         "datos": {"aplicables": excepciones}},
    ]
    resumen = (f"Inspeccioné {len(inspeccion['archivos'])} archivo(s) y {len(inspeccion['pares'])} par(es) "
               f"(destino: {', '.join(inspeccion['idiomas']['destino']) or 'sin idioma'}); "
               f"recuperé {len(reglas)} regla(s) y {len(excepciones)} excepción(es) aplicable(s).")
    return entradas, [e["id"] for e in entradas], resumen


def _refinar_reglas(ctx: dict, refin: dict) -> tuple[list[dict], list[str], str]:
    categorias = refin["detalle"].get("categorias", [])
    reglas_nuevas: list[dict] = []
    for cat in categorias:
        reglas_nuevas += search_l10n_qa_rules.invoke(
            {"query": cat.replace("_", " "), "categories": [cat], "top_k": TOP_K_REFINAMIENTO})
    entrada = {
        "id": _id(ctx["siguiente_id"]), "tipo": "reglas", "status": "confirmed",
        "descripcion": f"Reglas adicionales para las categorías: {', '.join(categorias)}.",
        "evidencia": {"herramienta": "search_l10n_qa_rules", "categorias": categorias,
                      "motivo_refinamiento": refin["motivo"]},
        "datos": {"reglas": _resumen_reglas(reglas_nuevas)},
    }
    return [entrada], [entrada["id"]], (
        f"Atendí el refinamiento: recuperé {len(reglas_nuevas)} regla(s) para {', '.join(categorias)}.")


def _refinar_archivos(ctx: dict, refin: dict) -> tuple[list[dict], list[str], str]:
    archivos = refin["detalle"].get("archivos", [])
    inspeccion = inspect_localization_files.invoke({"source_paths": [], "target_paths": archivos})
    # Los archivos de la reinspección se rotulan con su rol original si se conoce.
    entrada = {
        "id": _id(ctx["siguiente_id"]), "tipo": "reinspeccion", "status": "confirmed",
        "descripcion": f"Reinspección de {len(archivos)} archivo(s) por discrepancia con el análisis.",
        "evidencia": {"herramienta": "inspect_localization_files", "archivos": archivos,
                      "motivo_refinamiento": refin["motivo"]},
        "datos": {"archivos": inspeccion["archivos"]},
    }
    estados = ", ".join(f"{a['ruta']}: JSON {'válido' if a['json_valido'] else 'inválido o ilegible'}"
                        for a in inspeccion["archivos"])
    return [entrada], [entrada["id"]], f"Atendí el refinamiento reinspeccionando archivos ({estados})."


def research_node(state: L10nState) -> dict:
    """Nodo de LangGraph del Agente de Investigación."""
    ctx = contexto_investigacion(state)
    refin = ctx["refinamiento"]
    if refin is None and not ctx["ya_inspeccionado"]:
        entradas, ids, resumen = _investigacion_inicial(ctx)
        tipo = "investigacion_inicial"
    elif refin is not None and refin["tipo"] == "buscar_reglas":
        entradas, ids, resumen = _refinar_reglas(ctx, refin)
        tipo = "refinamiento_reglas"
    elif refin is not None and refin["tipo"] == "verificar_archivos":
        entradas, ids, resumen = _refinar_archivos(ctx, refin)
        tipo = "refinamiento_archivos"
    else:
        entradas, ids, resumen, tipo = [], [], "Sin trabajo pendiente para la investigación.", "sin_cambios"

    contribucion = crear_contribucion(
        paso=ctx["paso"], agente="research", tipo_analisis=tipo, resumen=resumen,
        hallazgos=ids,
        evidencias=[e["evidencia"].get("herramienta") for e in entradas],
        estado_hallazgos={"confirmed": len(entradas), "rejected": 0, "pending": 0},
        atendio_refinamiento=refin,
        datos={"entradas": ids},
    )
    actualizacion = {
        "research_findings": entradas,
        "agent_contributions": [contribucion],
        "step_count": 1,
        "messages": [AIMessage(content=f"[Investigación] {resumen}", name="research")],
    }
    if refin is not None:
        actualizacion["pending_refinement"] = None
    return actualizacion
