"""Inspección de archivos de localización (JSON).

Contiene utilidades de lectura segura de JSON y la herramienta
``inspect_localization_files`` usada por el Agente de Investigación.
Nunca modifica los archivos: solo los lee.
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import tool

PATRON_IDIOMA = re.compile(r"^[a-z]{2,3}([-_][A-Za-z0-9]{2,8})?$")


# --- Lectura segura ----------------------------------------------------------------
def sha256_archivo(ruta: str) -> Optional[str]:
    """Huella SHA-256 del contenido binario del archivo (None si no se puede leer)."""
    try:
        return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()
    except OSError:
        return None


def cargar_json(ruta: str) -> tuple[Any, Optional[dict]]:
    """Carga un JSON. Devuelve ``(datos, None)`` o ``(None, error)`` con error tipificado.

    Tipos de error: ``inexistente``, ``ilegible``, ``vacio``, ``json_invalido``.
    """
    p = Path(ruta)
    if not p.is_file():
        return None, {"tipo": "inexistente", "mensaje": f"El archivo no existe: {ruta}"}
    try:
        texto = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return None, {"tipo": "ilegible", "mensaje": f"No se pudo leer el archivo: {exc}"}
    if not texto.strip():
        return None, {"tipo": "vacio", "mensaje": f"El archivo está vacío: {ruta}"}
    try:
        return json.loads(texto), None
    except json.JSONDecodeError as exc:
        return None, {
            "tipo": "json_invalido",
            "mensaje": exc.msg,
            "linea": exc.lineno,
            "columna": exc.colno,
        }


# --- Utilidades sobre estructuras JSON -----------------------------------------------
def tipo_json(valor: Any) -> str:
    """Tipo JSON del valor. ``bool`` se distingue de ``number`` (en Python bool es int)."""
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "boolean"
    if isinstance(valor, (int, float)):
        return "number"
    if isinstance(valor, str):
        return "string"
    if isinstance(valor, list):
        return "array"
    if isinstance(valor, dict):
        return "object"
    return type(valor).__name__


def unir_ruta(base: str, clave: str) -> str:
    return f"{base}.{clave}" if base else clave


def aplanar_hojas(datos: Any, ruta: str = "") -> list[tuple[str, Any]]:
    """Lista de ``(ruta_json, valor)`` para cada valor hoja (escalares y contenedores vacíos)."""
    if isinstance(datos, dict) and datos:
        resultado: list[tuple[str, Any]] = []
        for clave, valor in datos.items():
            resultado.extend(aplanar_hojas(valor, unir_ruta(ruta, str(clave))))
        return resultado
    if isinstance(datos, list) and datos:
        resultado = []
        for i, valor in enumerate(datos):
            resultado.extend(aplanar_hojas(valor, f"{ruta}[{i}]"))
        return resultado
    return [(ruta or "$", datos)]


def nombres_de_claves(datos: Any) -> set[str]:
    """Todos los nombres de claves de objetos, recursivamente."""
    nombres: set[str] = set()
    if isinstance(datos, dict):
        for k, v in datos.items():
            nombres.add(str(k))
            nombres |= nombres_de_claves(v)
    elif isinstance(datos, list):
        for v in datos:
            nombres |= nombres_de_claves(v)
    return nombres


def ultima_clave(ruta_json: str) -> str:
    """Último nombre de clave de una ruta (ignora índices de array)."""
    sin_indices = re.sub(r"\[\d+\]", "", ruta_json)
    return sin_indices.split(".")[-1]


_TOKEN_RUTA = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolver_ruta(datos: Any, ruta_json: str) -> tuple[bool, Any]:
    """Resuelve una ruta ``a.b[0].c``. Devuelve ``(existe, valor)``.

    Limitación documentada: las claves que contienen puntos o corchetes no son resolubles.
    """
    if ruta_json in ("", "$"):
        return True, datos
    actual = datos
    for m in _TOKEN_RUTA.finditer(ruta_json):
        clave, indice = m.group(1), m.group(2)
        if indice is not None:
            i = int(indice)
            if not isinstance(actual, list) or i >= len(actual):
                return False, None
            actual = actual[i]
        else:
            if not isinstance(actual, dict) or clave not in actual:
                return False, None
            actual = actual[clave]
    return True, actual


# --- Nombres de archivo e idiomas ---------------------------------------------------------
def separar_nombre(ruta: str) -> tuple[str, Optional[str]]:
    """``home.es.json`` -> ``("home", "es")``. Sin idioma reconocible -> ``(base, None)``."""
    nombre = Path(ruta).name
    partes = nombre.split(".")
    if len(partes) >= 3 and partes[-1].lower() == "json" and PATRON_IDIOMA.match(partes[-2]):
        return ".".join(partes[:-2]), partes[-2].replace("_", "-")
    base = nombre[:-5] if nombre.lower().endswith(".json") else nombre
    return base, None


def expandir_rutas(rutas: list[str]) -> list[str]:
    """Expande carpetas a sus ``*.json`` (ordenados) y elimina duplicados conservando el orden."""
    resultado: list[str] = []
    for r in rutas:
        p = Path(r)
        elementos = sorted(str(x) for x in p.glob("*.json")) if p.is_dir() else [str(p)]
        for e in elementos:
            if e not in resultado:
                resultado.append(e)
    return resultado


def describir_archivo(ruta: str, rol: str) -> dict:
    """Describe un archivo: existencia, validez JSON, huella e idioma."""
    base, idioma = separar_nombre(ruta)
    datos, error = cargar_json(ruta)
    existe = Path(ruta).is_file()
    return {
        "ruta": str(ruta),
        "rol": rol,
        "base": base,
        "idioma": idioma,
        "existe": existe,
        "json_valido": error is None,
        "error": error,
        "sha256": sha256_archivo(ruta) if existe else None,
        "cantidad_claves_hoja": len(aplanar_hojas(datos)) if error is None else 0,
        "nombres_claves": sorted(nombres_de_claves(datos)) if error is None else [],
    }


def inspeccionar_archivos(rutas_origen: list[str], rutas_destino: list[str]) -> dict:
    """Inspecciona archivos de origen y destino y los empareja por nombre base.

    Emparejamiento: ``home.en.json`` (origen) con ``home.es.json`` / ``home.ar.json`` (destinos).
    """
    origenes = [describir_archivo(r, "origen") for r in expandir_rutas(rutas_origen)]
    destinos = [describir_archivo(r, "destino") for r in expandir_rutas(rutas_destino)]
    por_base: dict[str, dict] = {}
    for o in origenes:
        por_base.setdefault(o["base"], o)

    pares, sin_par = [], []
    bases_con_destino: set[str] = set()
    for d in destinos:
        o = por_base.get(d["base"])
        if o is None:
            sin_par.append({"ruta": d["ruta"], "rol": "destino",
                            "motivo": f"No hay archivo de origen con base «{d['base']}»."})
            continue
        bases_con_destino.add(d["base"])
        pares.append({
            "base": d["base"],
            "origen": o["ruta"], "destino": d["ruta"],
            "idioma_origen": o["idioma"], "idioma_destino": d["idioma"],
        })
    for base, o in por_base.items():
        if base not in bases_con_destino:
            sin_par.append({"ruta": o["ruta"], "rol": "origen",
                            "motivo": f"El origen «{base}» no tiene archivos de destino."})

    archivos = origenes + destinos
    return {
        "archivos": archivos,
        "pares": pares,
        "sin_par": sin_par,
        "idiomas": {
            "origen": sorted({a["idioma"] for a in origenes if a["idioma"]}),
            "destino": sorted({a["idioma"] for a in destinos if a["idioma"]}),
        },
        "no_analizables": [
            {"ruta": a["ruta"], "motivo": a["error"]["tipo"], "detalle": a["error"]["mensaje"]}
            for a in archivos
            if a["error"] and a["error"]["tipo"] in ("inexistente", "vacio", "ilegible")
        ],
    }


@tool
def inspect_localization_files(source_paths: list[str], target_paths: list[str]) -> dict:
    """Localiza e inspecciona los archivos JSON de origen y destino.

    Acepta rutas de archivos o de carpetas (se expanden a ``*.json``). Devuelve los archivos
    con su idioma, validez de JSON y huella SHA-256, los pares origen/destino detectados por
    nombre base (``base.<idioma>.json``), los archivos sin par y los no analizables.
    No modifica ningún archivo.
    """
    return inspeccionar_archivos(source_paths, target_paths)
