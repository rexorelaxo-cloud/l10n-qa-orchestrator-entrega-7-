"""Report Agent: redacta un resumen ejecutivo en español a partir de los hallazgos YA validados.

Reglas de seguridad del agente:
* Recibe solo metadatos de los hallazgos (id, severidad, categoría, archivo, ruta, mensaje); NO recibe el
  contenido de los archivos traducidos ni las evidencias con textos localizados.
* No crea ni modifica hallazgos: su salida es un texto aparte (``resumen_llm``) y el informe validado
  no se altera.
* En modo ``local`` no hay modelo: se genera un resumen determinista a partir de plantillas y se informa
  ``llamada_real = False``. Nunca se simula una llamada a un LLM.
"""
from __future__ import annotations

import time
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.factory import ManejadorLLM, costo_estimado
from app.observability import extraer_uso

MAX_HALLAZGOS_EN_PROMPT = 40

SISTEMA = (
    "Sos un asistente de Localization Engineering. Redactás en español, de forma breve y clara, un resumen "
    "ejecutivo de un QA ESTRUCTURAL de archivos JSON localizados que se importarán a un CMS. "
    "Reglas: usá únicamente los datos recibidos; no inventes hallazgos, rutas ni cifras; no propongas "
    "corregir ni reescribir traducciones (no es un QA lingüístico); indicá qué conviene revisar primero. "
    "Máximo 120 palabras."
)


def _datos_para_resumen(informe: dict) -> dict:
    hallazgos = []
    for sev in ("error", "warning", "info"):
        for h in informe.get("hallazgos_por_severidad", {}).get(sev, []):
            hallazgos.append({"id": h.get("id"), "severidad": sev, "categoria": h.get("categoria"),
                              "archivo": h.get("archivo"), "ruta_json": h.get("ruta_json"),
                              "mensaje": h.get("mensaje")})
    return {"estado": informe.get("estado_general"), "resumen": informe.get("resumen"),
            "total_hallazgos": len(hallazgos), "hallazgos": hallazgos[:MAX_HALLAZGOS_EN_PROMPT],
            "truncado": len(hallazgos) > MAX_HALLAZGOS_EN_PROMPT}


def resumen_determinista(datos: dict) -> str:
    r = datos.get("resumen") or {}
    partes = [f"Estado general del QA estructural: {datos.get('estado')}.",
              f"Errores: {r.get('errores', 0)}, advertencias: {r.get('advertencias', 0)}, "
              f"información: {r.get('informacion', 0)}."]
    errores = [h for h in datos["hallazgos"] if h["severidad"] == "error"]
    if errores:
        partes.append("Revisar primero: " + "; ".join(f"{h['archivo']} · {h['ruta_json']} ({h['categoria']})"
                                                       for h in errores[:5]) + ".")
    else:
        partes.append("No hay errores estructurales que bloqueen la importación.")
    return " ".join(partes)


def crear_nodo_redactor(handle: ManejadorLLM, settings: Settings) -> Callable:
    def llm_report_node(state: dict) -> dict:
        informe = state.get("final_report") or {}
        datos = _datos_para_resumen(informe)
        t0 = time.perf_counter()
        if not handle.es_real:
            texto = resumen_determinista(datos)
            resumen = {"texto": texto, "proveedor": handle.proveedor, "modelo": handle.modelo,
                       "llamada_real": False, "uso": {"informado": False},
                       "costo": {"origen": "no_aplica", "usd": 0.0, "nota": "Modo local: sin llamadas a un LLM."}}
        else:
            import json
            mensajes = [SystemMessage(content=SISTEMA),
                        HumanMessage(content="Datos del QA (JSON):\n" + json.dumps(datos, ensure_ascii=False))]
            respuesta = handle.modelo_chat.invoke(
                mensajes, config={"run_name": "llm_report", "metadata": {"job_id": state.get("job_id"),
                                                                         "thread_id": state.get("thread_id")}})
            uso = extraer_uso(respuesta)
            texto = respuesta.content if isinstance(respuesta.content, str) else str(respuesta.content)
            meta = getattr(respuesta, "response_metadata", {}) or {}
            resumen = {"texto": texto.strip(), "proveedor": handle.proveedor,
                       "modelo": meta.get("model_name") or handle.modelo, "llamada_real": True, "uso": uso,
                       "costo": costo_estimado(settings, uso.get("tokens_entrada"), uso.get("tokens_salida"))}
        resumen["duracion_s"] = round(time.perf_counter() - t0, 6)
        resumen["aclaracion"] = ("Texto redactado a partir de hallazgos ya validados; no es una fuente de "
                                 "hallazgos ni modifica el informe.")
        return {"resumen_llm": resumen}
    return llm_report_node
