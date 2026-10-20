"""Nodo de Validación final.

Comprueba la coherencia y completitud antes de terminar: contribuciones requeridas, estructura de
los hallazgos, evidencias reproducibles, fidelidad del informe, traducciones intactas y límite de
ejecución. Detecta conflictos entre agentes y, si es posible, pide una investigación específica.
Si no se resuelven, conserva ambos resultados y devuelve revisión pendiente.
"""
from pathlib import Path

from langchain_core.messages import AIMessage

from app.report import (
    ACLARACION_PASS, conflictos_vigentes, construir_informe, verificar_fidelidad,
)
from app.state import (
    L10nState, contribuciones_de, controles_solicitados, crear_contribucion, datos_analisis,
    entradas_investigacion, estado_archivos_investigacion, categorias_cubiertas,
)
from app.tools.file_inspector import sha256_archivo
from app.tools.l10n_rules import CONTROLES
from app.tools.structural_validator import verificar_evidencia

CAMPOS_HALLAZGO = ("id", "severidad", "categoria", "regla", "archivo", "ruta_json", "mensaje", "evidencia", "estado")
PASOS_NECESARIOS_PARA_REFINAR = 4  # supervisor + especialista + supervisor + validación


def _ver(id_: str, descripcion: str, ok: bool, detalle: str) -> dict:
    return {"id": id_, "descripcion": descripcion, "ok": ok, "detalle": detalle}


def detectar_conflictos(state: dict) -> list[dict]:
    """Compara lo que afirma la investigación con lo que reporta el análisis (sin descartar nada)."""
    estado = estado_archivos_investigacion(state)
    datos = datos_analisis(state)
    conflictos: dict[str, dict] = {}
    for h in state.get("analysis_findings", []):
        if h["categoria"] != "json_invalido":
            continue
        ruta = h["evidencia"].get("archivo_invalido", h["archivo"])
        info = estado.get(ruta)
        if info and info["json_valido"]:
            cid = f"C-JSON-{Path(ruta).name}"
            conflictos[cid] = {
                "id": cid, "tipo": "json_valido_vs_invalido", "archivo": ruta, "estado": "sin_resolver",
                "descripcion": f"La investigación indica que «{ruta}» es JSON válido, pero el análisis lo reporta inválido.",
                "investigacion": {"json_valido": True, "sha256": info["sha256"]},
                "analisis": {"hallazgo": h["id"], "error": h["evidencia"].get("error")}}
    for etiqueta in datos["pares_comparados"]:
        for ruta in etiqueta.split(" → "):
            info = estado.get(ruta)
            if info and not info["json_valido"]:
                cid = f"C-COMP-{Path(ruta).name}"
                conflictos[cid] = {
                    "id": cid, "tipo": "invalido_vs_comparado", "archivo": ruta, "estado": "sin_resolver",
                    "descripcion": f"La investigación indica que «{ruta}» no es JSON válido, pero el análisis lo comparó con éxito.",
                    "investigacion": {"json_valido": False, "error": info["error"]},
                    "analisis": {"par_comparado": etiqueta}}
    return list(conflictos.values())


def validation_node(state: L10nState) -> dict:
    """Nodo de LangGraph de validación final."""
    n = state.get("step_count", 0) + 1
    max_pasos = state.get("max_steps", 12)
    hallazgos = [dict(h) for h in state.get("analysis_findings", [])]
    datos = datos_analisis(state)
    verificaciones: list[dict] = []

    # --- Completitud: ¿qué faltó? ---------------------------------------------------------
    incompleto_por: list[str] = []
    if not state.get("user_request", "").strip():
        incompleto_por.append("La solicitud del usuario está vacía.")
    elif not entradas_investigacion(state, "inspeccion"):
        incompleto_por.append("No se realizó la investigación (falta contribución del Agente de Investigación).")
    elif not any(a for a in entradas_investigacion(state, "inspeccion")[0]["datos"]["archivos"]):
        incompleto_por.append("No se recibió ningún archivo de entrada.")
    elif not contribuciones_de(state, "analysis"):
        incompleto_por.append("No se realizó el análisis (falta contribución del Agente de Análisis).")
    else:
        pendientes = [c for c in controles_solicitados(state) if c not in datos["controles_ejecutados"]]
        if pendientes:
            incompleto_por.append("Controles solicitados sin ejecutar: " + ", ".join(pendientes) + ".")
        if not datos["pares_comparados"] and not any(h["categoria"] == "json_invalido" for h in hallazgos):
            incompleto_por.append("No se pudo comparar ningún par origen/destino.")
    verificaciones.append(_ver(
        "contribuciones", "Están presentes las contribuciones necesarias (investigación y análisis)",
        not any("falta contribución" in m or "No se recibió" in m or "vacía" in m for m in incompleto_por),
        "; ".join(incompleto_por) or "Investigación, análisis y supervisión contribuyeron."))

    # --- Estructura de los hallazgos ------------------------------------------------------
    invalidos = []
    for h in hallazgos:
        faltan = [c for c in CAMPOS_HALLAZGO if c not in h]
        if faltan or h.get("severidad") not in ("error", "warning", "info") \
                or h.get("estado") not in ("confirmed", "rejected", "pending"):
            invalidos.append(h.get("id", "?"))
            h["estado"], h["motivo_rechazo"] = "rejected", f"Estructura inválida (faltan: {', '.join(faltan) or 'valores fuera de rango'})."
    verificaciones.append(_ver("estructura", "Los hallazgos tienen una estructura válida", not invalidos,
                               f"Hallazgos con estructura inválida: {', '.join(invalidos)}." if invalidos
                               else f"{len(hallazgos)} hallazgo(s) con estructura válida."))

    # --- Evidencia reproducible -------------------------------------------------------------
    sin_evidencia = []
    for h in hallazgos:
        if h["estado"] == "rejected":
            continue
        ok, detalle = verificar_evidencia(h)
        if not ok:
            sin_evidencia.append(h["id"])
            h["estado"], h["motivo_rechazo"] = "rejected", f"Evidencia no reproducible: {detalle}"
    verificaciones.append(_ver("evidencia", "Cada hallazgo tiene evidencia suficiente para reproducirlo", not sin_evidencia,
                               f"Evidencia no reproducible en: {', '.join(sin_evidencia)}." if sin_evidencia
                               else "Todas las evidencias se reprodujeron releyendo los archivos."))

    # --- Traducciones intactas ----------------------------------------------------------------
    modificados = []
    huellas_investigacion = {}
    for e in entradas_investigacion(state, "inspeccion"):
        huellas_investigacion.update(e["evidencia"]["huellas_sha256"])
    for ruta, v in datos["integridad"].items():
        actual = sha256_archivo(ruta)
        referencia = huellas_investigacion.get(ruta, v["sha256_antes"])
        if actual != v["sha256_antes"] or actual != referencia or v["sha256_despues"] != v["sha256_antes"]:
            modificados.append(ruta)
    verificaciones.append(_ver("traducciones_intactas", "Las traducciones y archivos originales permanecen intactos", not modificados,
                               f"Archivos con huella distinta: {', '.join(modificados)}." if modificados
                               else f"{len(datos['integridad'])} archivo(s) con huella SHA-256 idéntica antes y después."))

    # --- Límite de ejecución --------------------------------------------------------------------
    verificaciones.append(_ver("limite", "No se superó el límite de ejecución", n <= max_pasos,
                               f"Ejecución {n} de {max_pasos} permitidas."))

    # --- Conflictos entre agentes -------------------------------------------------------------------
    detectados = detectar_conflictos(state)
    previos = {c["id"]: c for c in conflictos_vigentes(state)}
    nuevos_registros = []
    for c in detectados:
        if previos.get(c["id"], {}).get("estado") != "sin_resolver":
            nuevos_registros.append(c)
    for cid, c in previos.items():
        if c["estado"] == "sin_resolver" and cid not in {d["id"] for d in detectados}:
            nuevos_registros.append({**c, "estado": "resuelto",
                                     "resolucion": "Tras la reinvestigación ambos agentes coinciden."})

    refinamiento = None
    if detectados:
        archivos = sorted({c["archivo"] for c in detectados})
        clave = f"research:verificar_archivos:{'|'.join(archivos)}"
        ya_pedido = any(r.get("clave") == clave for r in state.get("refinement_log", []))
        if not ya_pedido and n + PASOS_NECESARIOS_PARA_REFINAR <= max_pasos:
            refinamiento = {"destino": "research", "tipo": "verificar_archivos", "origen": "validation",
                            "motivo": "Conflicto entre investigación y análisis sobre la validez JSON de: " + ", ".join(archivos),
                            "detalle": {"archivos": archivos}, "clave": clave}

    contribucion_base = dict(paso=n, agente="validation", tipo_analisis="validacion_final")
    if refinamiento:
        resumen = "Se detectó un conflicto entre agentes; se solicita una investigación específica antes de cerrar."
        validacion = {"verificaciones": verificaciones, "fallos": [], "incompleto_por": incompleto_por,
                      "conflictos": detectados, "requiere_refinamiento": True, "solicitud_refinamiento": refinamiento}
        return {
            "validation_results": validacion,
            "conflicts": nuevos_registros,
            "pending_refinement": refinamiento,
            "refinement_log": [refinamiento],
            "next_agent": "supervisor",
            "task_completed": False,
            "step_count": 1,
            "agent_contributions": [crear_contribucion(**contribucion_base, resumen=resumen,
                                                       solicitud_refinamiento=refinamiento,
                                                       datos={"conflictos": [c["id"] for c in detectados]})],
            "messages": [AIMessage(content=f"[Validación] {resumen}", name="validation")],
        }

    # --- Cierre: informe final ---------------------------------------------------------------------------
    vista = {**state, "analysis_findings": hallazgos, "conflicts": list(state.get("conflicts", [])) + nuevos_registros,
             "step_count": n}
    validacion = {"verificaciones": verificaciones, "fallos": [], "incompleto_por": incompleto_por,
                  "conflictos": detectados, "requiere_refinamiento": False, "solicitud_refinamiento": None,
                  "hallazgos_rechazados": [h["id"] for h in hallazgos if h["estado"] == "rejected"]}
    informe = construir_informe(vista, validacion)
    ok, detalle = verificar_fidelidad(informe, hallazgos)
    verificaciones.append(_ver("fidelidad", "El informe refleja fielmente los hallazgos", ok, detalle))
    validacion["fallos"] = [v["id"] for v in verificaciones if not v["ok"]]
    informe = construir_informe(vista, validacion)   # se recalcula el estado con el resultado final de las verificaciones
    validacion["estado"] = informe["estado_general"]

    resumen = (f"Validación final: {informe['estado_general']} ({informe['estado_general_es']}). "
               f"Verificaciones fallidas: {len(validacion['fallos'])}. Conflictos abiertos: "
               f"{sum(c['estado'] == 'sin_resolver' for c in conflictos_vigentes(vista))}.")
    return {
        "validation_results": validacion,
        "conflicts": nuevos_registros,
        "final_report": informe,
        "next_agent": "END",
        "task_completed": True,
        "step_count": 1,
        "agent_contributions": [crear_contribucion(
            **contribucion_base, resumen=resumen,
            hallazgos=[h["id"] for h in hallazgos if h["estado"] == "rejected"],
            estado_hallazgos={"confirmed": sum(h["estado"] == "confirmed" for h in hallazgos),
                              "rejected": sum(h["estado"] == "rejected" for h in hallazgos),
                              "pending": sum(h["estado"] == "pending" for h in hallazgos)},
            datos={"estado": informe["estado_general"], "verificaciones": [v["id"] for v in verificaciones]})],
        "messages": [AIMessage(content=f"[Validación] {resumen}", name="validation")],
    }


def enrutar_validacion(state: L10nState):
    """Arista condicional posterior a la validación: vuelve al supervisor o termina."""
    return "END" if state.get("task_completed") else "supervisor"
