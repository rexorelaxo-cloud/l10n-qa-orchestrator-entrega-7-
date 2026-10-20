"""Base de conocimiento local de reglas de QA estructural para L10n Engineering.

Contiene las reglas consultables, la configuración de excepciones y la herramienta
``search_l10n_qa_rules`` del Agente de Investigación. Todo funciona sin servicios externos.

Nota de alcance: son reglas de QA ESTRUCTURAL y de preparación para importación.
No hay reglas de calidad lingüística (gramática, terminología, estilo).
"""
import re
import unicodedata
from typing import Optional

from langchain_core.tools import tool

FUENTE_KB = "Base de conocimiento local (tools/l10n_rules.py)"

# --- Configuración y excepciones ---------------------------------------------------------
CONFIG_POR_DEFECTO: dict = {
    # Las claves cuyo nombre empieza con estos prefijos son metadatos sensibles a la importación:
    # su valor debe coincidir con el del origen (el CMS los usa para reconciliar ítems).
    "metadata_prefijos": ["_gpiInternalData-"],
    # Excepciones por clave EXPLÍCITA (nunca por prefijo completo). Se documentan en el informe.
    "excepciones": [
        {
            "id": "EXC-001",
            "clave": "_gpiInternalData-toolName",
            "aplica_a": "metadatos",
            "efecto": "permitir_valor_distinto",
            "justificacion": "El nombre de la herramienta puede variar legítimamente por idioma "
                             "o por el flujo de traducción; no afecta la importación.",
        },
        {
            "id": "EXC-002",
            "clave": "_gpiInternalData-rootPath",
            "aplica_a": "metadatos",
            "efecto": "permitir_valor_distinto",
            "justificacion": "La ruta raíz del contenido incluye el segmento de idioma "
                             "(p. ej. /en/ frente a /es/), por lo que difiere legítimamente.",
        },
    ],
}

# --- Controles solicitables y categorías de reglas que los respaldan -------------------------
CONTROLES = {
    "claves_faltantes": ["correspondencia_claves"],
    "claves_inesperadas": ["correspondencia_claves"],
    "tipos_datos": ["tipos_datos"],
    "estructuras": ["estructuras_anidadas"],
    "placeholders": ["placeholders"],
    "saltos_linea": ["saltos_linea"],
    "metadatos": ["metadatos", "excepciones"],
}

# Fragmentos (sin acentos) que activan cada control al aparecer en la solicitud.
_PISTAS_CONTROLES = {
    "claves_faltantes": ["falt", "ausent", "missing"],
    "claves_inesperadas": ["inesperad", "sobrant", "unexpected", "extra"],
    "tipos_datos": ["tipo", "type"],
    "estructuras": ["anidad", "array", "arreglo", "objeto", "nested"],
    "placeholders": ["placeholder", "variable", "marcador"],
    "saltos_linea": ["salto", "linea", "newline", "escapad"],
    "metadatos": ["metadat", "gpi", "importacion"],
}

# --- Reglas ------------------------------------------------------------------------------
REGLAS: list[dict] = [
    {"id": "REG-CLV-001", "categoria": "correspondencia_claves",
     "titulo": "Toda clave del origen debe existir en el destino",
     "descripcion": "Una clave presente en el archivo de origen y ausente en el destino es un "
                    "error estructural: el CMS no podrá importar el campo. No se completa ni se "
                    "copia el valor del origen.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["clave faltante", "claves faltantes", "ausente", "missing key", "correspondencia de claves", "campo faltante"]},
    {"id": "REG-CLV-002", "categoria": "correspondencia_claves",
     "titulo": "No debe haber claves inesperadas en el destino",
     "descripcion": "Una clave presente en el destino y ausente en el origen es un error "
                    "estructural: el CMS puede rechazar campos desconocidos. Solo se informa; "
                    "no se elimina.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["clave inesperada", "claves inesperadas", "clave extra", "campo sobrante", "unexpected key"]},
    {"id": "REG-TIP-001", "categoria": "tipos_datos",
     "titulo": "El tipo de dato JSON debe coincidir entre origen y destino",
     "descripcion": "string, number, boolean, null, array y object deben coincidir. Un número "
                    "convertido en cadena (3 frente a \"3\") es un error. boolean no es number.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["tipo de dato", "tipos de datos", "diferencia de tipo", "type mismatch", "string number boolean"]},
    {"id": "REG-TIP-002", "categoria": "tipos_datos",
     "titulo": "Un valor de texto vacío en el destino requiere revisión",
     "descripcion": "Si el origen tiene texto y el destino una cadena vacía, se emite una "
                    "advertencia: puede ser un campo sin traducir o un problema de exportación. "
                    "No se completa el valor.",
     "severidad_por_defecto": "warning",
     "palabras_clave": ["valor vacio", "cadena vacia", "texto vacio", "campo vacio"]},
    {"id": "REG-EST-001", "categoria": "estructuras_anidadas",
     "titulo": "Los objetos anidados deben conservar la jerarquía",
     "descripcion": "La comparación es recursiva: cada objeto anidado debe tener las mismas "
                    "claves e igual jerarquía. Cada hallazgo informa su ruta JSON completa.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["objeto anidado", "objetos anidados", "jerarquia", "estructura anidada", "ruta json", "nested object"]},
    {"id": "REG-EST-002", "categoria": "estructuras_anidadas",
     "titulo": "Los arrays deben tener la misma longitud y estructura por posición",
     "descripcion": "Los elementos de un array se comparan por índice. Una longitud distinta es "
                    "un error estructural; el orden de los elementos no se reordena.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["array", "arreglo", "lista", "longitud del array", "elementos del array"]},
    {"id": "REG-PH-001", "categoria": "placeholders",
     "titulo": "Los placeholders deben conservarse sin alteraciones",
     "descripcion": "Los marcadores {nombre}, {{nombre}}, ${nombre}, %s, %d y %1$s deben aparecer "
                    "en el destino exactamente como en el origen (mismo nombre y cantidad). Un "
                    "placeholder traducido, renombrado, faltante o sobrante es un error.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["placeholder", "placeholders", "variable de sustitucion", "variables de sustitucion", "marcador", "llaves"]},
    {"id": "REG-PH-002", "categoria": "placeholders",
     "titulo": "El orden de los placeholders puede variar",
     "descripcion": "El orden de los placeholders dentro de la frase puede cambiar por la "
                    "gramática o por la dirección de escritura (p. ej. árabe, RTL). Se compara "
                    "el conjunto de placeholders, no su posición.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["orden de placeholders", "rtl", "arabe", "posicion del placeholder"]},
    {"id": "REG-NL-001", "categoria": "saltos_linea",
     "titulo": "No se intercambian saltos de línea reales y secuencias \\n literales",
     "descripcion": "Un salto de línea real (carácter LF) y la secuencia literal de dos "
                    "caracteres barra invertida + n no son equivalentes. Reemplazar uno por el "
                    "otro rompe el formato al importar y es un error.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["salto de linea", "saltos de linea", "caracter escapado", "caracteres escapados", "newline", "secuencia literal"]},
    {"id": "REG-NL-002", "categoria": "saltos_linea",
     "titulo": "Una cantidad distinta de saltos de línea requiere revisión",
     "descripcion": "Si el tipo de salto se conserva pero la cantidad difiere, se emite una "
                    "advertencia: puede ser una reformulación legítima o una pérdida de formato.",
     "severidad_por_defecto": "warning",
     "palabras_clave": ["cantidad de saltos", "discrepancia de saltos", "saltos de linea distintos", "formato de parrafos"]},
    {"id": "REG-MET-001", "categoria": "metadatos",
     "titulo": "Los metadatos _gpiInternalData-* deben coincidir con el origen",
     "descripcion": "Las claves con prefijo _gpiInternalData- son metadatos sensibles a la "
                    "importación (el CMS reconcilia ítems con ellas). Su valor debe ser igual al "
                    "del origen, salvo excepciones configuradas por clave.",
     "severidad_por_defecto": "error",
     "palabras_clave": ["metadato", "metadatos", "metadata", "_gpiinternaldata", "gpi", "importacion", "cms", "sitecore", "umbraco"]},
    {"id": "REG-EXC-001", "categoria": "excepciones",
     "titulo": "Excepción configurable: _gpiInternalData-toolName",
     "descripcion": "El valor de _gpiInternalData-toolName puede diferir del origen. Se registra "
                    "como información y se documenta la excepción aplicada.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["excepcion", "excepciones", "toolname", "_gpiinternaldata-toolname", "configurable"]},
    {"id": "REG-EXC-002", "categoria": "excepciones",
     "titulo": "Excepción configurable: _gpiInternalData-rootPath",
     "descripcion": "El valor de _gpiInternalData-rootPath puede diferir del origen porque incluye "
                    "el idioma. Se registra como información y se documenta la excepción.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["excepcion", "excepciones", "rootpath", "_gpiinternaldata-rootpath", "ruta raiz", "configurable"]},
    {"id": "REG-EXC-003", "categoria": "excepciones",
     "titulo": "Las excepciones se aplican por clave explícita y se documentan",
     "descripcion": "No se ignoran todos los metadatos. Cada excepción identifica una clave "
                    "concreta, tiene justificación y aparece en el informe cuando se aplica.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["excepcion", "excepciones", "documentar excepciones", "ignorar metadatos", "clave explicita"]},
    {"id": "REG-DIF-001", "categoria": "diferencias_legitimas",
     "titulo": "Un texto igual al origen no es un error por sí mismo",
     "descripcion": "Marcas, nombres propios, siglas y términos universales pueden coincidir con "
                    "el inglés. No se marcan como errores ni como traducciones faltantes.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["texto identico", "igual al ingles", "marca", "nombre propio", "no es un error", "diferencia legitima"]},
    {"id": "REG-DIF-002", "categoria": "diferencias_legitimas",
     "titulo": "Una longitud distinta no es un error por sí misma",
     "descripcion": "Los textos traducidos suelen ser más largos o más cortos que el origen. "
                    "La longitud no se usa como criterio de error; solo se informa como dato.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["longitud", "mas largo", "mas corto", "expansion del texto", "diferencia legitima"]},
    {"id": "REG-DIF-003", "categoria": "diferencias_legitimas",
     "titulo": "El orden de las claves de un objeto no es relevante",
     "descripcion": "Dos objetos con las mismas claves en distinto orden son estructuralmente "
                    "equivalentes. Los elementos de un array sí se comparan por posición.",
     "severidad_por_defecto": "info",
     "palabras_clave": ["orden de claves", "orden de las claves", "diferencia legitima"]},
]

_PALABRAS_VACIAS = {
    "para", "como", "pero", "sobre", "entre", "desde", "hasta", "cada", "esta", "este",
    "estos", "estas", "debe", "deben", "tiene", "otro", "otra", "otros", "otras", "sino",
    "solo", "puede", "pueden", "ante", "bajo", "tras", "segun", "sin", "con", "los", "las",
    "una", "uno", "unos", "unas", "del", "que", "por", "son", "ser", "mas", "muy", "sus",
}


# --- Búsqueda -------------------------------------------------------------------------
def normalizar_texto(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar de forma robusta."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _raices(texto: str) -> set[str]:
    palabras = re.findall(r"[a-z0-9_]+", normalizar_texto(texto))
    return {p[:5] for p in palabras if len(p) > 3 and p not in _PALABRAS_VACIAS}


def obtener_regla(id_regla: str) -> Optional[dict]:
    return next((r for r in REGLAS if r["id"] == id_regla), None)


def buscar_reglas(consulta: str, categorias: Optional[list[str]] = None, top_k: int = 5) -> list[dict]:
    """Búsqueda por coincidencia de raíces de palabras con ponderación.

    Pesos: palabras clave x3, título x2, descripción x1. Con ``categorias`` se restringen los
    candidatos a esas categorías; si ninguna coincide con la consulta se devuelven igualmente
    (puntaje 0) para poder responder a un pedido de refinamiento por categoría.
    """
    raices_consulta = _raices(consulta)
    resultados = []
    for regla in REGLAS:
        if categorias and regla["categoria"] not in categorias:
            continue
        r_clave = set().union(*[_raices(k) for k in regla["palabras_clave"]]) if regla["palabras_clave"] else set()
        r_titulo = _raices(regla["titulo"])
        r_desc = _raices(regla["descripcion"])
        puntaje = sum(3 * (q in r_clave) + 2 * (q in r_titulo) + 1 * (q in r_desc) for q in raices_consulta)
        if puntaje > 0 or categorias:
            resultados.append({**regla, "puntaje": puntaje, "fuente": FUENTE_KB})
    resultados.sort(key=lambda r: (-r["puntaje"], r["id"]))
    return resultados[: max(1, top_k)]


def inferir_controles(solicitud: str) -> list[str]:
    """Deduce qué controles estructurales pide la solicitud (todos si no reconoce ninguno)."""
    texto = normalizar_texto(solicitud)
    pedidos = [c for c, pistas in _PISTAS_CONTROLES.items() if any(p in texto for p in pistas)]
    return pedidos or list(CONTROLES)


def categorias_requeridas(controles: list[str]) -> list[str]:
    """Categorías de reglas necesarias para respaldar los controles (más diferencias legítimas)."""
    categorias: list[str] = []
    for c in controles:
        for cat in CONTROLES.get(c, []):
            if cat not in categorias:
                categorias.append(cat)
    categorias.append("diferencias_legitimas")
    return categorias


def excepciones_aplicables(config: dict, nombres_claves: set[str]) -> list[dict]:
    """Excepciones configuradas cuyas claves aparecen realmente en los archivos de origen."""
    return [e for e in config.get("excepciones", []) if e["clave"] in nombres_claves]


@tool
def search_l10n_qa_rules(query: str, categories: Optional[list[str]] = None, top_k: int = 5) -> list[dict]:
    """Consulta la base de conocimiento local de reglas de QA estructural de L10n.

    Devuelve las reglas más relevantes para ``query`` (ordenadas por puntaje), con su id,
    categoría, título, descripción y severidad por defecto. ``categories`` limita la búsqueda
    (correspondencia_claves, tipos_datos, estructuras_anidadas, placeholders, saltos_linea,
    metadatos, excepciones, diferencias_legitimas).
    """
    return buscar_reglas(query, categories, top_k)
