"""Construcción y exportación del informe final de QA (JSON y Markdown)."""
import json
from pathlib import Path

from app.state import (
    ESTADO_GENERAL_ES, ESTADO_HALLAZGO_ES, SEVERIDAD_ES, contribuciones_de, datos_analisis,
    primera_inspeccion, reglas_recuperadas,
)
from app.tools.l10n_rules import obtener_regla

ALCANCE = (
    "Este sistema realiza exclusivamente QA estructural y validación de preparación para "
    "importación. No realiza QA lingüístico, no corrige ni completa traducciones y no modifica "
    "los archivos originales."
)

DESCRIPCION_ESTADO = {
    "PASS": "Los archivos superaron los controles estructurales configurados.",
    "FAIL": "Se detectaron errores estructurales confirmados que impedirían o comprometerían la importación.",
    "NEEDS_REVIEW": "No hay errores confirmados, pero quedan advertencias, discrepancias entre agentes "
                    "o hallazgos rechazados que requieren revisión humana.",
    "INCOMPLETE": "No fue posible completar el análisis (límite de pasos, entradas inválidas o archivos "
                  "no analizables); el resultado NO debe considerarse validado.",
}

ACLARACION_PASS = (
    "PASS significa que los archivos superaron los controles estructurales configurados. No significa "
    "que las traducciones sean lingüísticamente correctas ni que se haya realizado una importación "
    "real al CMS."
)

RECOMENDACION_POR_CATEGORIA = {
    "clave_faltante": "Agregar en el archivo de destino las claves faltantes (el equipo de traducción debe aportar el texto).",
    "clave_inesperada": "Revisar las claves inesperadas: eliminarlas del destino o confirmar si el origen debe actualizarse.",
    "tipo_distinto": "Corregir el tipo de dato del destino para que coincida con el origen (p. ej. número, no cadena).",
    "longitud_array_distinta": "Igualar la cantidad de elementos de los arrays entre origen y destino.",
    "valor_vacio": "Revisar los valores vacíos del destino: pueden ser campos sin traducir o un fallo de exportación.",
    "placeholder_alterado": "Restaurar los placeholders del destino exactamente como en el origen (no se traducen).",
    "salto_linea_tipo_distinto": "Restaurar el tipo de salto de línea del origen (real frente a secuencia literal \\n).",
    "salto_linea_cantidad_distinta": "Revisar si la diferencia en la cantidad de saltos de línea es intencional.",
    "metadato_distinto": "Restaurar el valor del metadato sensible a la importación según el origen, o configurar una excepción justificada.",
    "json_invalido": "Corregir la sintaxis del JSON y volver a ejecutar el QA.",
}


def conteo_por_severidad(hallazgos: list[dict]) -> dict:
    return {
        "errores": sum(h["severidad"] == "error" for h in hallazgos),
        "advertencias": sum(h["severidad"] == "warning" for h in hallazgos),
        "informacion": sum(h["severidad"] == "info" for h in hallazgos),
        "total": len(hallazgos),
    }


def conflictos_vigentes(state: dict) -> list[dict]:
    """Último estado de cada conflicto (los registros anteriores se conservan en el estado)."""
    ultimo: dict[str, dict] = {}
    for c in state.get("conflicts", []):
        ultimo[c["id"]] = c
    return list(ultimo.values())


def calcular_estado(state: dict, validacion: dict) -> str:
    hallazgos = state.get("analysis_findings", [])
    datos = datos_analisis(state)
    conflictos_abiertos = [c for c in conflictos_vigentes(state) if c["estado"] == "sin_resolver"]
    if (validacion.get("incompleto_por") or datos["no_analizables"]
            or not datos["pares_comparados"] and not any(h["categoria"] == "json_invalido" for h in hallazgos)):
        return "INCOMPLETE"
    if any(h.get("severidad") == "error" and h.get("estado") != "rejected" for h in hallazgos):
        return "FAIL"
    if (any(h["severidad"] == "warning" for h in hallazgos) or conflictos_abiertos
            or any(h.get("estado") == "rejected" for h in hallazgos) or validacion.get("fallos")):
        return "NEEDS_REVIEW"
    return "PASS"


def construir_informe(state: dict, validacion: dict) -> dict:
    hallazgos = state.get("analysis_findings", [])
    datos = datos_analisis(state)
    inspeccion = primera_inspeccion(state) or {"pares": [], "idiomas": {"origen": [], "destino": []}}
    estado = calcular_estado(state, validacion)
    por_severidad = {s: [h for h in hallazgos if h.get("severidad") == s] for s in ("error", "warning", "info")}
    # Hallazgos con severidad inválida: nunca se descartan en silencio, se listan aparte.
    invalidos = [h for h in hallazgos if h.get("severidad") not in ("error", "warning", "info")]

    ids_reglas = sorted({h["regla"] for h in hallazgos if h.get("regla")} | set(reglas_recuperadas(state)))
    reglas = []
    for rid in ids_reglas:
        regla = obtener_regla(rid)
        reglas.append({"id": rid, "titulo": regla["titulo"] if regla else "Regla interna de sintaxis JSON"})

    excepciones = []
    for e in datos["excepciones_aplicadas"]:
        if e not in excepciones:
            excepciones.append(e)

    pendientes = []
    for n in datos["no_analizables"]:
        pendientes.append(f"Archivo no analizable ({n['motivo']}): {n['ruta']}. {n.get('detalle', '')}".strip())
    for c in conflictos_vigentes(state):
        if c["estado"] == "sin_resolver":
            pendientes.append(f"Conflicto sin resolver {c['id']}: {c['descripcion']}")
    for h in por_severidad["warning"]:
        pendientes.append(f"Advertencia {h['id']} pendiente de revisión: {h.get('archivo', '?')} · {h.get('ruta_json', '?')}.")
    for h in hallazgos:
        if h.get("estado") == "rejected":
            pendientes.append(f"Hallazgo {h.get('id', '?')} rechazado por la validación: {h.get('motivo_rechazo', 'sin motivo')}.")
    if validacion.get("incompleto_por"):
        pendientes.append("El análisis no se completó: " + "; ".join(validacion["incompleto_por"]))
    pendientes += [f"Incertidumbre: {u}" for u in state.get("uncertainties", [])]
    pendientes += [f"Error de ejecución: {e}" for e in state.get("errors", [])]

    categorias = []
    for h in por_severidad["error"] + por_severidad["warning"]:
        if h.get("categoria") not in categorias:
            categorias.append(h.get("categoria"))
    recomendaciones = [RECOMENDACION_POR_CATEGORIA[c] for c in categorias if c in RECOMENDACION_POR_CATEGORIA]
    if estado == "INCOMPLETE":
        recomendaciones.append("Resolver las entradas no analizables o aumentar el límite de pasos y volver a ejecutar el QA.")
    if estado == "PASS":
        recomendaciones.append("Los controles estructurales pasaron; se sugiere una prueba de importación real en un entorno de preproducción del CMS.")

    contribuyentes = sorted({c["agente"] for c in state.get("agent_contributions", [])} | {"validation"})
    return {
        "titulo": "Informe de QA estructural de localización",
        "alcance": ALCANCE,
        "solicitud": state.get("user_request", ""),
        "estado_general": estado,
        "estado_general_es": ESTADO_GENERAL_ES[estado],
        "descripcion_estado": DESCRIPCION_ESTADO[estado],
        "aclaracion": ACLARACION_PASS,
        "archivos_analizados": [p for p in inspeccion["pares"] if f"{p['origen']} → {p['destino']}" in datos["pares_comparados"]],
        "archivos_no_analizados": datos["no_analizables"],
        "pares_sin_comparar": datos["pares_sin_comparar"],
        "idiomas_analizados": {"origen": inspeccion["idiomas"]["origen"], "destino": inspeccion["idiomas"]["destino"]},
        "resumen": conteo_por_severidad(hallazgos),
        "hallazgos_por_severidad": por_severidad,
        "hallazgos_invalidos": invalidos,
        "reglas_aplicadas": reglas,
        "excepciones_aplicadas": excepciones,
        "agentes_contribuyentes": contribuyentes,
        "controles_ejecutados": datos["controles_ejecutados"],
        "controles_omitidos": datos["controles_omitidos"],
        "validacion": validacion,
        "conflictos": conflictos_vigentes(state),
        "problemas_pendientes": pendientes,
        "recomendaciones": recomendaciones,
        "pasos_ejecutados": state.get("step_count", 0),
        "max_pasos": state.get("max_steps"),
        "decisiones_supervisor": state.get("routing_log", []),
    }


def verificar_fidelidad(informe: dict, hallazgos: list[dict]) -> tuple[bool, str]:
    """Recuenta los hallazgos reales y los compara con lo que afirma el informe."""
    esperado = conteo_por_severidad(hallazgos)
    en_informe = informe["resumen"]
    ids_informe = sorted(h["id"] for lista in informe["hallazgos_por_severidad"].values() for h in lista) \
        + sorted(h.get("id", "?") for h in informe.get("hallazgos_invalidos", []))
    ids_informe = sorted(ids_informe)
    ids_reales = sorted(h.get("id", "?") for h in hallazgos)
    if esperado != en_informe:
        return False, f"El resumen del informe {en_informe} no coincide con los hallazgos reales {esperado}."
    if ids_informe != ids_reales:
        return False, "Los hallazgos listados en el informe no coinciden con los hallazgos reales."
    return True, "El informe refleja fielmente los hallazgos producidos."


# --- Exportación ------------------------------------------------------------------------------
def informe_a_markdown(informe: dict) -> str:
    r = informe["resumen"]
    L = [f"# {informe['titulo']}", "",
         f"> {informe['alcance']}", "",
         f"## Estado general: **{informe['estado_general']}** ({informe['estado_general_es']})", "",
         informe["descripcion_estado"], "", f"_{informe['aclaracion']}_", "",
         "## Solicitud", "", informe["solicitud"] or "(vacía)", "",
         "## Resumen", "",
         f"- Errores: **{r['errores']}**", f"- Advertencias: **{r['advertencias']}**",
         f"- Información: **{r['informacion']}**", f"- Pasos ejecutados: {informe['pasos_ejecutados']} de {informe['max_pasos']}", "",
         f"- Idiomas de origen: {', '.join(informe['idiomas_analizados']['origen']) or '—'}",
         f"- Idiomas de destino: {', '.join(informe['idiomas_analizados']['destino']) or '—'}",
         f"- Agentes que contribuyeron: {', '.join(informe['agentes_contribuyentes'])}",
         f"- Controles ejecutados: {', '.join(informe['controles_ejecutados']) or '—'}", "",
         "## Archivos analizados", ""]
    L += [f"- `{p['origen']}` → `{p['destino']}` ({p['idioma_origen']} → {p['idioma_destino']})"
          for p in informe["archivos_analizados"]] or ["- (ninguno)"]
    if informe["archivos_no_analizados"]:
        L += ["", "### Archivos no analizados", ""]
        L += [f"- `{n['ruta']}`: {n['motivo']} — {n.get('detalle', '')}" for n in informe["archivos_no_analizados"]]
    for sev, titulo in (("error", "Errores"), ("warning", "Advertencias"), ("info", "Información")):
        lista = informe["hallazgos_por_severidad"][sev]
        L += ["", f"## {titulo} ({len(lista)})", ""]
        if not lista:
            L.append("Sin hallazgos.")
        for h in lista:
            L += ["", f"### {h['id']} · {h['categoria']} · `{h['ruta_json']}`",
                  f"- Archivo: `{h['archivo']}` (idioma: {h.get('idioma') or '—'})",
                  f"- Regla: {h['regla']} · Estado: {ESTADO_HALLAZGO_ES[h['estado']]}",
                  f"- {h['mensaje']}",
                  "- Evidencia: `" + json.dumps(h["evidencia"], ensure_ascii=False) + "`"]
            if h.get("excepcion_aplicada"):
                L.append(f"- Excepción aplicada: {h['excepcion_aplicada']['id']} — {h['excepcion_aplicada']['justificacion']}")
    if informe.get("hallazgos_invalidos"):
        L += ["", f"## Hallazgos rechazados por estructura inválida ({len(informe['hallazgos_invalidos'])})", ""]
        L += [f"- {h.get('id', '?')}: {h.get('motivo_rechazo', 'estructura inválida')}" for h in informe["hallazgos_invalidos"]]
    L += ["", "## Reglas aplicadas", ""] + [f"- **{x['id']}**: {x['titulo']}" for x in informe["reglas_aplicadas"]]
    L += ["", "## Excepciones aplicadas", ""]
    L += [f"- **{e['id']}** en `{e['ruta_json']}` ({e['archivo']}): {e['justificacion']}"
          for e in informe["excepciones_aplicadas"]] or ["- Ninguna."]
    v = informe["validacion"]
    L += ["", "## Validación final", ""]
    L += [f"- [{'x' if c['ok'] else ' '}] {c['descripcion']} — {c['detalle']}" for c in v.get("verificaciones", [])]
    if informe["conflictos"]:
        L += ["", "## Conflictos entre agentes", ""]
        L += [f"- **{c['id']}** ({c['estado']}): {c['descripcion']}" for c in informe["conflictos"]]
    L += ["", "## Problemas pendientes", ""] + ([f"- {p}" for p in informe["problemas_pendientes"]] or ["- Ninguno."])
    L += ["", "## Recomendaciones", ""] + [f"- {x}" for x in informe["recomendaciones"]]
    L += ["", "## Decisiones del Supervisor", ""]
    L += [f"{d['paso']}. → `{d['ruta']}`: {d['motivo']}" for d in informe["decisiones_supervisor"]]
    return "\n".join(L) + "\n"


def guardar_informe(informe: dict, carpeta: str) -> dict[str, str]:
    """Guarda el informe como ``informe_qa.json`` e ``informe_qa.md``."""
    destino = Path(carpeta)
    destino.mkdir(parents=True, exist_ok=True)
    ruta_json, ruta_md = destino / "informe_qa.json", destino / "informe_qa.md"
    ruta_json.write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")
    ruta_md.write_text(informe_a_markdown(informe), encoding="utf-8")
    return {"json": str(ruta_json), "markdown": str(ruta_md)}
