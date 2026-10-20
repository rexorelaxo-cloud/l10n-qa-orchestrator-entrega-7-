"""Validación estructural determinista de archivos JSON de localización.

Funciones puras (testeables) y tres herramientas LangChain para el Agente de Análisis:

* ``compare_json_structure``: claves faltantes/inesperadas, tipos, arrays y valores vacíos.
* ``check_placeholders_and_newlines``: placeholders y saltos de línea / caracteres escapados.
* ``validate_rules_and_exceptions``: metadatos sensibles, excepciones y diferencias legítimas.

Reglas de oro: nunca se modifica un archivo, nunca se completa ni se corrige una traducción y
no se emite un error por que un texto coincida con el origen o difiera en longitud.
"""
import re
from collections import Counter
from typing import Any, Optional

from langchain_core.tools import tool

from app.tools.file_inspector import (
    aplanar_hojas, cargar_json, resolver_ruta, tipo_json, ultima_clave, unir_ruta,
)
from app.tools.l10n_rules import CONFIG_POR_DEFECTO, CONTROLES

PATRON_PLACEHOLDER = re.compile(
    r"\{\{\s*[^{}]+?\s*\}\}"      # {{nombre}}
    r"|\$\{[^{}]+\}"              # ${nombre}
    r"|\{[^{}\s][^{}]*\}"         # {nombre} / {0}
    r"|%(?:\d+\$)?[sd]"           # %s %d %1$s
)


def _h(categoria: str, severidad: str, regla: str, ruta_json: str, mensaje: str, evidencia: dict) -> dict:
    """Hallazgo 'crudo' de una herramienta (el Agente de Análisis le agrega id, archivo y estado)."""
    return {"categoria": categoria, "severidad": severidad, "regla": regla,
            "ruta_json": ruta_json, "mensaje": mensaje, "evidencia": evidencia}


def _controles(controles: Optional[list[str]]) -> set[str]:
    return set(controles) if controles else set(CONTROLES)


def _es_metadato(ruta_json: str, config: dict) -> bool:
    clave = ultima_clave(ruta_json)
    return any(clave.startswith(p) for p in config.get("metadata_prefijos", []))


# =======================================================================================
# 1. Comparación estructural
# =======================================================================================
def comparar_estructura(origen: Any, destino: Any, controles: Optional[list[str]] = None,
                        ruta: str = "") -> list[dict]:
    """Compara recursivamente origen y destino (claves, tipos, arrays, valores vacíos)."""
    activos = _controles(controles)
    hallazgos: list[dict] = []
    t_o, t_d = tipo_json(origen), tipo_json(destino)
    r = ruta or "$"

    if t_o != t_d:
        if "tipos_datos" in activos:
            hallazgos.append(_h(
                "tipo_distinto", "error", "REG-TIP-001", r,
                f"El tipo de dato difiere: origen «{t_o}», destino «{t_d}».",
                {"tipo_origen": t_o, "tipo_destino": t_d,
                 "valor_origen": origen, "valor_destino": destino}))
        return hallazgos

    if t_o == "object":
        for clave in origen:
            if clave not in destino and "claves_faltantes" in activos:
                hallazgos.append(_h(
                    "clave_faltante", "error", "REG-CLV-001", unir_ruta(ruta, str(clave)),
                    "La clave existe en el origen pero falta en el destino.",
                    {"presente_en_origen": True, "presente_en_destino": False,
                     "valor_origen": origen[clave]}))
        for clave in destino:
            if clave not in origen and "claves_inesperadas" in activos:
                hallazgos.append(_h(
                    "clave_inesperada", "error", "REG-CLV-002", unir_ruta(ruta, str(clave)),
                    "La clave existe en el destino pero no en el origen.",
                    {"presente_en_origen": False, "presente_en_destino": True,
                     "valor_destino": destino[clave]}))
        for clave in origen:
            if clave in destino:
                hallazgos.extend(comparar_estructura(origen[clave], destino[clave], controles,
                                                     unir_ruta(ruta, str(clave))))
    elif t_o == "array":
        if len(origen) != len(destino) and "estructuras" in activos:
            hallazgos.append(_h(
                "longitud_array_distinta", "error", "REG-EST-002", r,
                f"El array tiene {len(origen)} elementos en el origen y {len(destino)} en el destino.",
                {"longitud_origen": len(origen), "longitud_destino": len(destino)}))
        for i in range(min(len(origen), len(destino))):
            hallazgos.extend(comparar_estructura(origen[i], destino[i], controles, f"{ruta}[{i}]"))
    elif t_o == "string":
        if "tipos_datos" in activos and origen.strip() and not destino.strip():
            hallazgos.append(_h(
                "valor_vacio", "warning", "REG-TIP-002", r,
                "El destino es una cadena vacía aunque el origen tiene texto.",
                {"valor_origen": origen, "valor_destino": destino}))
    return hallazgos


# =======================================================================================
# 2. Placeholders y saltos de línea
# =======================================================================================
def extraer_placeholders(texto: str) -> list[str]:
    return PATRON_PLACEHOLDER.findall(texto)


def contar_saltos(texto: str) -> dict:
    """Saltos reales (LF, CRLF o CR cuentan uno) y secuencias literales ``\\n`` (barra + n)."""
    return {"reales": len(re.findall(r"\r\n|\n|\r", texto)), "literales": texto.count("\\n")}


def _pares_de_cadenas(origen: Any, destino: Any, config: dict) -> list[tuple[str, str, str]]:
    mapa_destino = dict(aplanar_hojas(destino))
    pares = []
    for ruta, valor in aplanar_hojas(origen):
        if isinstance(valor, str) and isinstance(mapa_destino.get(ruta), str) \
                and not _es_metadato(ruta, config):
            pares.append((ruta, valor, mapa_destino[ruta]))
    return pares


def comparar_placeholders_y_saltos(origen: Any, destino: Any, controles: Optional[list[str]] = None,
                                   config: Optional[dict] = None) -> list[dict]:
    activos = _controles(controles)
    config = config or CONFIG_POR_DEFECTO
    hallazgos: list[dict] = []
    for ruta, v_o, v_d in _pares_de_cadenas(origen, destino, config):
        if "placeholders" in activos:
            c_o, c_d = Counter(extraer_placeholders(v_o)), Counter(extraer_placeholders(v_d))
            if c_o != c_d:
                faltantes = sorted((c_o - c_d).elements())
                sobrantes = sorted((c_d - c_o).elements())
                hallazgos.append(_h(
                    "placeholder_alterado", "error", "REG-PH-001", ruta,
                    "Los placeholders del destino no coinciden con los del origen.",
                    {"faltantes_en_destino": faltantes, "sobrantes_en_destino": sobrantes,
                     "placeholders_origen": sorted(c_o.elements()),
                     "placeholders_destino": sorted(c_d.elements()),
                     "valor_origen": v_o, "valor_destino": v_d}))
        if "saltos_linea" in activos:
            s_o, s_d = contar_saltos(v_o), contar_saltos(v_d)
            if s_o == s_d:
                continue
            ev = {"saltos_origen": s_o, "saltos_destino": s_d, "valor_origen": v_o, "valor_destino": v_d}
            literal_por_real = s_d["literales"] > s_o["literales"] and s_d["reales"] < s_o["reales"]
            real_por_literal = s_d["reales"] > s_o["reales"] and s_d["literales"] < s_o["literales"]
            if literal_por_real:
                hallazgos.append(_h(
                    "salto_linea_tipo_distinto", "error", "REG-NL-001", ruta,
                    "Un salto de línea real del origen aparece como secuencia literal «\\n» en el destino.", ev))
            elif real_por_literal:
                hallazgos.append(_h(
                    "salto_linea_tipo_distinto", "error", "REG-NL-001", ruta,
                    "Una secuencia literal «\\n» del origen aparece como salto de línea real en el destino.", ev))
            else:
                hallazgos.append(_h(
                    "salto_linea_cantidad_distinta", "warning", "REG-NL-002", ruta,
                    "La cantidad de saltos de línea difiere; requiere revisión.", ev))
    return hallazgos


# =======================================================================================
# 3. Metadatos, excepciones y diferencias legítimas
# =======================================================================================
def _es_texto_traducible(valor: str) -> bool:
    v = valor.strip()
    if not re.search(r"[A-Za-z]", v):
        return False
    return not (v.startswith("/") or v.lower().startswith("http"))


def validar_reglas_y_excepciones(origen: Any, destino: Any, controles: Optional[list[str]] = None,
                                 config: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """Valida metadatos sensibles con excepciones y resume diferencias legítimas.

    Devuelve ``(hallazgos, excepciones_aplicadas)``.
    """
    activos = _controles(controles)
    config = config or CONFIG_POR_DEFECTO
    por_clave = {e["clave"]: e for e in config.get("excepciones", [])}
    mapa_destino = dict(aplanar_hojas(destino))
    hallazgos: list[dict] = []
    aplicadas: list[dict] = []

    if "metadatos" in activos:
        for ruta, v_o in aplanar_hojas(origen):
            if not _es_metadato(ruta, config) or ruta not in mapa_destino:
                continue  # si falta la clave, ya lo informa la comparación estructural
            v_d = mapa_destino[ruta]
            if v_o == v_d:
                continue
            clave = ultima_clave(ruta)
            exc = por_clave.get(clave)
            if exc is not None:
                aplicada = {"id": exc["id"], "clave": clave, "ruta_json": ruta,
                            "efecto": exc["efecto"], "justificacion": exc["justificacion"]}
                aplicadas.append(aplicada)
                hallazgos.append(_h(
                    "metadato_diferencia_permitida", "info", "REG-EXC-003", ruta,
                    f"El metadato difiere del origen pero está permitido por la excepción {exc['id']}.",
                    {"valor_origen": v_o, "valor_destino": v_d, "excepcion": exc["id"]}) | {"excepcion_aplicada": aplicada})
            else:
                hallazgos.append(_h(
                    "metadato_distinto", "error", "REG-MET-001", ruta,
                    "Un metadato sensible a la importación difiere del origen y no tiene excepción.",
                    {"valor_origen": v_o, "valor_destino": v_d}))

    # Diferencias legítimas: solo informativas, nunca errores.
    identicos, longitud = [], []
    for ruta, v_o in aplanar_hojas(origen):
        v_d = mapa_destino.get(ruta)
        if not isinstance(v_o, str) or not isinstance(v_d, str) or _es_metadato(ruta, config):
            continue
        if not _es_texto_traducible(v_o):
            continue
        if v_o == v_d:
            identicos.append(ruta)
        elif v_o.strip() and v_d.strip():
            ratio = len(v_d) / len(v_o)
            if ratio < 0.7 or ratio > 1.4:
                longitud.append(ruta)
    if identicos or longitud:
        hallazgos.append(_h(
            "diferencia_legitima", "info", "REG-DIF-001", "$",
            f"Diferencias legítimas que NO se consideran errores: {len(identicos)} valor(es) "
            f"idéntico(s) al origen y {len(longitud)} con longitud notablemente distinta.",
            {"valores_identicos_al_origen": identicos, "longitud_notablemente_distinta": longitud,
             "reglas": ["REG-DIF-001", "REG-DIF-002"]}))
    return hallazgos, aplicadas


# =======================================================================================
# Verificación de evidencias (reproducibilidad)
# =======================================================================================
def verificar_evidencia(hallazgo: dict) -> tuple[bool, str]:
    """Reproduce la evidencia de un hallazgo leyendo de nuevo los archivos reales."""
    ev = hallazgo.get("evidencia") or {}
    if not ev:
        return False, "El hallazgo no tiene evidencia."
    if hallazgo.get("categoria") == "json_invalido":
        _, error = cargar_json(ev.get("archivo_invalido", hallazgo.get("archivo", "")))
        return (error is not None and error.get("tipo") == "json_invalido",
                "El archivo sigue sin poder analizarse como JSON." if error else
                "El archivo ahora es JSON válido: la evidencia no se reproduce.")
    destino, error = cargar_json(hallazgo.get("archivo", ""))
    if error is not None:
        return False, f"No se puede releer el archivo: {error['mensaje']}"
    existe, valor = resolver_ruta(destino, hallazgo.get("ruta_json", "$"))
    if "presente_en_destino" in ev and existe != ev["presente_en_destino"]:
        return False, "La presencia de la clave en el destino no coincide con la evidencia."
    if "valor_destino" in ev and not (existe and valor == ev["valor_destino"]):
        return False, "El valor actual del destino no coincide con la evidencia."
    if "longitud_destino" in ev and not (existe and isinstance(valor, list) and len(valor) == ev["longitud_destino"]):
        return False, "La longitud actual del array no coincide con la evidencia."
    if "presente_en_origen" in ev and hallazgo.get("archivo_origen"):
        origen, err_o = cargar_json(hallazgo["archivo_origen"])
        if err_o is not None:
            return False, f"No se puede releer el origen: {err_o['mensaje']}"
        existe_o, _ = resolver_ruta(origen, hallazgo["ruta_json"])
        if existe_o != ev["presente_en_origen"]:
            return False, "La presencia de la clave en el origen no coincide con la evidencia."
    return True, "La evidencia se reproduce con los archivos actuales."


# =======================================================================================
# Herramientas LangChain (procesan archivos reales)
# =======================================================================================
def _cargar_par(ruta_origen: str, ruta_destino: str) -> tuple[Any, Any, Optional[dict]]:
    origen, e_o = cargar_json(ruta_origen)
    destino, e_d = cargar_json(ruta_destino)
    if e_o or e_d:
        return None, None, {"origen": e_o, "destino": e_d}
    return origen, destino, None


@tool
def compare_json_structure(source_path: str, target_path: str,
                           controls: Optional[list[str]] = None) -> dict:
    """Compara recursivamente la estructura de un JSON de origen y uno de destino.

    Detecta claves faltantes, claves inesperadas, diferencias de tipo de dato, longitudes
    de array distintas y valores de texto vacíos. ``controls`` limita los controles
    (claves_faltantes, claves_inesperadas, tipos_datos, estructuras). No modifica archivos.
    """
    origen, destino, error = _cargar_par(source_path, target_path)
    if error:
        return {"hallazgos": [], "error_entrada": error}
    return {"hallazgos": comparar_estructura(origen, destino, controls), "error_entrada": None}


@tool
def check_placeholders_and_newlines(source_path: str, target_path: str,
                                    controls: Optional[list[str]] = None) -> dict:
    """Detecta discrepancias de placeholders y de saltos de línea / caracteres escapados.

    Compara el conjunto de placeholders ({x}, {{x}}, ${x}, %s, %d) y distingue el salto de
    línea real de la secuencia literal «\\n». No modifica archivos.
    """
    origen, destino, error = _cargar_par(source_path, target_path)
    if error:
        return {"hallazgos": [], "error_entrada": error}
    return {"hallazgos": comparar_placeholders_y_saltos(origen, destino, controls), "error_entrada": None}


@tool
def validate_rules_and_exceptions(source_path: str, target_path: str,
                                  controls: Optional[list[str]] = None,
                                  config: Optional[dict] = None) -> dict:
    """Valida metadatos sensibles a la importación aplicando las excepciones configuradas.

    Devuelve además un resumen informativo de diferencias legítimas (valores idénticos al
    origen, longitudes distintas) que no se consideran errores, y la lista de excepciones
    efectivamente aplicadas. No modifica archivos.
    """
    origen, destino, error = _cargar_par(source_path, target_path)
    if error:
        return {"hallazgos": [], "excepciones_aplicadas": [], "error_entrada": error}
    hallazgos, aplicadas = validar_reglas_y_excepciones(origen, destino, controls, config)
    return {"hallazgos": hallazgos, "excepciones_aplicadas": aplicadas, "error_entrada": None}
