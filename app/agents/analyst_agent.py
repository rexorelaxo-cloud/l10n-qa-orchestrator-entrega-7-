"""Agente de Análisis.

Responsabilidad: análisis estructural determinista sobre los pares origen/destino que identificó
la investigación, usando las reglas y excepciones recuperadas. Produce hallazgos con evidencia.
NO edita archivos ni corrige traducciones.
"""
from langchain_core.messages import AIMessage

from app.state import (
    L10nState, categorias_cubiertas, contar_por_estado, controles_solicitados,
    crear_contribucion, entradas_investigacion, primera_inspeccion, reglas_recuperadas,
)
from app.tools.file_inspector import cargar_json, sha256_archivo
from app.tools.l10n_rules import CONTROLES
from app.tools.structural_validator import (
    check_placeholders_and_newlines, compare_json_structure, validate_rules_and_exceptions,
)

INSTRUCCIONES_ANALISIS = (
    "Eres el Agente de Análisis de L10n QA. Compará de forma determinista cada par origen/destino "
    "con las reglas recibidas. Nunca modifiques archivos, no completes ni corrijas traducciones, "
    "no consideres error un texto solo por coincidir con el origen o por su longitud, y respaldá "
    "cada hallazgo con evidencia verificable."
)

ESTADO_POR_SEVERIDAD = {"error": "confirmed", "warning": "pending", "info": "confirmed"}


def contexto_analisis(state: dict) -> dict:
    """Contexto MÍNIMO que recibe el Agente de Análisis (aislamiento de contexto).

    Recibe solo: la solicitud, los pares de archivos, las reglas aplicables, las excepciones y su
    propio refinamiento. No recibe el historial de mensajes ni las contribuciones de otros agentes.
    """
    inspeccion = primera_inspeccion(state) or {"pares": [], "sin_par": []}
    solicitados = controles_solicitados(state)
    cubiertas = categorias_cubiertas(state)
    ejecutables = [c for c in solicitados if all(cat in cubiertas for cat in CONTROLES[c][:1])]
    sin_regla = [c for c in solicitados if c not in ejecutables]

    refin = state.get("pending_refinement")
    refin = refin if refin and refin.get("destino") == "analysis" else None
    if refin and refin.get("detalle", {}).get("controles"):
        ejecutables = [c for c in refin["detalle"]["controles"] if c in ejecutables]

    excepciones = []
    for e in entradas_investigacion(state, "excepciones"):
        excepciones += e["datos"]["aplicables"]
    config = dict(state.get("config", {}))
    config["excepciones"] = excepciones   # el análisis solo usa las excepciones que la investigación halló aplicables

    return {
        "instrucciones": INSTRUCCIONES_ANALISIS,
        "solicitud": state.get("user_request", ""),
        "pares": inspeccion["pares"],
        "sin_par": inspeccion["sin_par"],
        "controles_a_ejecutar": ejecutables,
        "controles_sin_regla": sin_regla,
        "reglas": [{"id": r["id"], "categoria": r["categoria"], "titulo": r["titulo"]}
                   for r in reglas_recuperadas(state).values()],
        "config": config,
        "refinamiento": refin,
        "hallazgos_previos": len(state.get("analysis_findings", [])),
        "paso": state.get("step_count", 0) + 1,
    }


def _hallazgo(n: int, raw: dict, par: dict) -> dict:
    return {
        "id": f"F-{n:03d}",
        "severidad": raw["severidad"],
        "categoria": raw["categoria"],
        "regla": raw["regla"],
        "archivo": par["destino"],
        "archivo_origen": par["origen"],
        "idioma": par.get("idioma_destino"),
        "ruta_json": raw["ruta_json"],
        "mensaje": raw["mensaje"],
        "evidencia": raw["evidencia"],
        "estado": ESTADO_POR_SEVERIDAD[raw["severidad"]],
        "excepcion_aplicada": raw.get("excepcion_aplicada"),
        "agente": "analysis",
    }


def _hallazgo_json_invalido(n: int, ruta: str, rol: str, error: dict, par: dict) -> dict:
    return {
        "id": f"F-{n:03d}", "severidad": "error", "categoria": "json_invalido", "regla": "REG-SINTAXIS",
        "archivo": ruta,
        "archivo_origen": par["origen"], "idioma": par.get("idioma_destino"), "ruta_json": "$",
        "mensaje": f"El archivo de {rol} no es JSON válido: {error['mensaje']} "
                   f"(línea {error.get('linea')}, columna {error.get('columna')}).",
        "evidencia": {"archivo_invalido": ruta, "rol": rol, "error": error},
        "estado": "confirmed", "excepcion_aplicada": None, "agente": "analysis",
    }


def analizar_pares(ctx: dict) -> dict:
    """Ejecuta las herramientas de análisis sobre cada par. Devuelve hallazgos y datos de trazabilidad."""
    controles = ctx["controles_a_ejecutar"]
    config = ctx["config"]
    hallazgos: list[dict] = []
    comparados, sin_comparar, no_analizables, aplicadas, errores = [], [], [], [], []
    integridad: dict[str, dict] = {}
    contador = ctx["hallazgos_previos"]

    for s in ctx["sin_par"]:
        if s["rol"] == "destino":   # destino sin origen: no hay contra qué comparar
            no_analizables.append({"ruta": s["ruta"], "motivo": "sin_par", "detalle": s["motivo"]})
        else:                       # origen sin destinos: dato informativo, no es un error de entrada
            sin_comparar.append({"par": s["ruta"], "motivo": s["motivo"]})

    for par in ctx["pares"]:
        for ruta in (par["origen"], par["destino"]):
            integridad[ruta] = {"sha256_antes": sha256_archivo(ruta), "sha256_despues": None}
        etiqueta = f"{par['origen']} → {par['destino']}"
        _, e_origen = cargar_json(par["origen"])
        _, e_destino = cargar_json(par["destino"])

        if e_origen and e_origen["tipo"] != "json_invalido":
            no_analizables.append({"ruta": par["origen"], "motivo": e_origen["tipo"], "detalle": e_origen["mensaje"]})
            sin_comparar.append({"par": etiqueta, "motivo": "origen no analizable"})
            continue
        if e_destino and e_destino["tipo"] != "json_invalido":
            no_analizables.append({"ruta": par["destino"], "motivo": e_destino["tipo"], "detalle": e_destino["mensaje"]})
            sin_comparar.append({"par": etiqueta, "motivo": "destino no analizable"})
            continue
        if e_origen:   # origen con JSON inválido: se informa el error y no se puede comparar
            contador += 1
            hallazgos.append(_hallazgo_json_invalido(contador, par["origen"], "origen", e_origen, par))
            no_analizables.append({"ruta": par["origen"], "motivo": "json_invalido", "detalle": e_origen["mensaje"]})
            sin_comparar.append({"par": etiqueta, "motivo": "origen con JSON inválido"})
            continue
        if e_destino:  # destino con JSON inválido: error confirmado de sintaxis
            contador += 1
            hallazgos.append(_hallazgo_json_invalido(contador, par["destino"], "destino", e_destino, par))
            sin_comparar.append({"par": etiqueta, "motivo": "destino con JSON inválido"})
            continue

        try:
            raws = []
            raws += compare_json_structure.invoke(
                {"source_path": par["origen"], "target_path": par["destino"], "controls": controles})["hallazgos"]
            raws += check_placeholders_and_newlines.invoke(
                {"source_path": par["origen"], "target_path": par["destino"], "controls": controles})["hallazgos"]
            r = validate_rules_and_exceptions.invoke(
                {"source_path": par["origen"], "target_path": par["destino"],
                 "controls": controles, "config": config})
            raws += r["hallazgos"]
            aplicadas += [{**a, "archivo": par["destino"]} for a in r["excepciones_aplicadas"]]
        except Exception as exc:  # error de herramienta: se informa y el par queda sin analizar
            errores.append(f"Error de herramienta en {etiqueta}: {exc}")
            no_analizables.append({"ruta": par["destino"], "motivo": "error_herramienta", "detalle": str(exc)})
            sin_comparar.append({"par": etiqueta, "motivo": "error de herramienta"})
            continue
        for raw in raws:
            contador += 1
            hallazgos.append(_hallazgo(contador, raw, par))
        comparados.append(etiqueta)

    for ruta, valores in integridad.items():
        valores["sha256_despues"] = sha256_archivo(ruta)

    return {"hallazgos": hallazgos, "comparados": comparados, "sin_comparar": sin_comparar,
            "no_analizables": no_analizables, "aplicadas": aplicadas, "errores": errores,
            "integridad": integridad}


def analysis_node(state: L10nState) -> dict:
    """Nodo de LangGraph del Agente de Análisis."""
    ctx = contexto_analisis(state)
    res = analizar_pares(ctx)
    hallazgos = res["hallazgos"]
    cuenta = {s: sum(1 for h in hallazgos if h["severidad"] == s) for s in ("error", "warning", "info")}
    resumen = (f"Analicé {len(res['comparados'])} par(es) con los controles {', '.join(ctx['controles_a_ejecutar']) or 'ninguno'}: "
               f"{cuenta['error']} error(es), {cuenta['warning']} advertencia(s), {cuenta['info']} información. "
               f"No analizables: {len(res['no_analizables'])}.")
    refin = ctx["refinamiento"]
    contribucion = crear_contribucion(
        paso=ctx["paso"], agente="analysis",
        tipo_analisis="refinamiento_controles" if refin else "analisis_estructural", resumen=resumen,
        hallazgos=[h["id"] for h in hallazgos],
        evidencias=["compare_json_structure", "check_placeholders_and_newlines",
                    "validate_rules_and_exceptions"] + [r["id"] for r in ctx["reglas"]],
        estado_hallazgos=contar_por_estado(hallazgos),
        atendio_refinamiento=refin,
        datos={
            "controles_ejecutados": ctx["controles_a_ejecutar"],
            "controles_omitidos": ctx["controles_sin_regla"],
            "pares_comparados": res["comparados"],
            "pares_sin_comparar": res["sin_comparar"],
            "no_analizables": res["no_analizables"],
            "excepciones_aplicadas": res["aplicadas"],
            "integridad": res["integridad"],
        },
    )
    actualizacion = {
        "analysis_findings": hallazgos,
        "agent_contributions": [contribucion],
        "step_count": 1,
        "messages": [AIMessage(content=f"[Análisis] {resumen}", name="analysis")],
    }
    if res["errores"]:
        actualizacion["errors"] = res["errores"]
    if refin is not None:
        actualizacion["pending_refinement"] = None
    return actualizacion
