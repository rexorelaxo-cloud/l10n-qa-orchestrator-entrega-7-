"""Human-in-the-Loop: aprobación humana antes de la operación crítica (importación simulada al CMS).

Flujo en el grafo::

    llm_report → approval_gate ──(interrupt)──► [el grafo se detiene y se guarda el checkpoint en Redis]
                      │
          Command(resume={decision, approver, ...})
                      ├─ approve → execute_import → END
                      └─ reject  → END (la operación crítica NUNCA se ejecuta)

``approval_gate`` usa ``langgraph.types.interrupt``. Al reanudarse, LangGraph vuelve a ejecutar el nodo
desde el principio, por eso todo lo que va antes de ``interrupt`` es puro (sin efectos secundarios).
La operación crítica vive en un nodo aparte, ``execute_import``, que solo se alcanza tras una aprobación.

IMPORTANTE: no se realiza ninguna importación real a Sitecore, Umbraco ni a otro CMS. La «importación»
es la escritura local de un manifiesto JSON verificable en ``OUTPUT_DIR/importaciones/<job_id>.json``,
creado de forma exclusiva (``open(..., 'x')``) para que no pueda ejecutarse dos veces.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from langgraph.types import interrupt

from app.tools.file_inspector import sha256_archivo

ESTADOS_ELEGIBLES = {"PASS", "NEEDS_REVIEW"}
DECISIONES = {"approve", "reject"}
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def construir_paquete(state: dict) -> dict:
    """Vista previa (determinista) del paquete que se autorizaría. No tiene efectos secundarios."""
    informe = state.get("final_report") or {}
    archivos = []
    for par in informe.get("archivos_analizados", []):
        destino = par.get("destino")
        archivos.append({"origen": par.get("origen"), "destino": destino, "sha256_destino": sha256_archivo(destino)
                         if destino else None})
    return {"job_id": state.get("job_id"), "thread_id": state.get("thread_id"),
            "estado_qa": informe.get("estado_general"), "resumen": informe.get("resumen"), "archivos": archivos}


def nodo_puerta_aprobacion(state: dict) -> dict:
    if not state.get("require_approval"):
        return {"import_status": "NO_SOLICITADA"}
    estado = (state.get("final_report") or {}).get("estado_general")
    if estado not in ESTADOS_ELEGIBLES:
        return {"import_status": "NO_ELEGIBLE",
                "import_result": {"motivo": f"El estado del QA es {estado}: el paquete no se puede autorizar."}}

    paquete = construir_paquete(state)
    decision = interrupt({
        "tipo": "aprobacion_importacion_cms",
        "mensaje": "Se requiere autorización humana para preparar el paquete de importación al CMS (simulada).",
        "paquete": paquete,
    })
    # Se llega aquí solo cuando el grafo se reanuda con Command(resume=...).
    if not isinstance(decision, dict) or decision.get("decision") not in DECISIONES:
        # Una respuesta inválida jamás se interpreta como aprobación.
        return {"import_status": "RECHAZADA", "approval": {"decision": "reject", "invalida": True,
                                                            "comment": "Respuesta de aprobación inválida."}}
    aprobada = decision["decision"] == "approve"
    return {"import_status": "APROBADA" if aprobada else "RECHAZADA", "approval": dict(decision)}


def enrutar_tras_aprobacion(state: dict) -> Literal["execute_import", "END"]:
    return "execute_import" if state.get("import_status") == "APROBADA" else "END"


def crear_nodo_importacion(output_dir: Path) -> Callable:
    def execute_import_node(state: dict) -> dict:
        # Defensa en profundidad: aunque el enrutamiento ya lo garantiza, se revalida la aprobación.
        if state.get("import_status") != "APROBADA" or (state.get("approval") or {}).get("decision") != "approve":
            raise RuntimeError("Operación crítica bloqueada: no existe una aprobación válida.")
        job_id = state.get("job_id") or ""
        if not _JOB_ID.match(job_id):
            raise ValueError("job_id inválido para la operación de importación.")
        carpeta = Path(output_dir) / "importaciones"
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / f"{job_id}.json"
        manifiesto = {"job_id": job_id, "thread_id": state.get("thread_id"), "simulada": True,
                      "aviso": "Importación SIMULADA: no se contactó a ningún CMS.",
                      "ejecutada_en": datetime.now(timezone.utc).isoformat(),
                      "aprobacion": state.get("approval"), "paquete": construir_paquete(state)}
        try:
            with open(ruta, "x", encoding="utf-8") as f:    # creación exclusiva: no se ejecuta dos veces
                json.dump(manifiesto, f, ensure_ascii=False, indent=2)
            repetida = False
        except FileExistsError:
            repetida = True                                  # reanudación tras una interrupción: idempotente
        return {"import_status": "EJECUTADA",
                "import_result": {"manifiesto": str(ruta), "simulada": True, "ya_existia": repetida}}
    return execute_import_node
