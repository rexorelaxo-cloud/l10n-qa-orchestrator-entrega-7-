"""Punto de entrada por línea de comandos del L10n QA Orchestrator.

Ejemplo:
    python main.py --origen fixtures/source --destino fixtures/targets
"""
import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from app.graph import graph
from app.report import guardar_informe, informe_a_markdown
from app.state import MAX_PASOS_POR_DEFECTO, crear_estado_inicial

RAIZ = Path(__file__).resolve().parent.parent
SOLICITUD_EJEMPLO = (
    "Compará los archivos JSON de origen en inglés con sus versiones traducidas al español y al árabe. "
    "Investigá las reglas estructurales aplicables, identificá claves faltantes o inesperadas, "
    "diferencias de tipos de datos, problemas con placeholders y discrepancias en los saltos de línea. "
    "Determiná si los archivos cumplen los controles estructurales configurados para una posible "
    "importación al CMS y generá un informe de QA con evidencias."
)
CODIGOS_SALIDA = {"PASS": 0, "FAIL": 1, "NEEDS_REVIEW": 2, "INCOMPLETE": 3}


def ejecutar_qa(
    solicitud: str,
    origen: list[str],
    destino: list[str],
    max_pasos: Optional[int] = None,
    carpeta_salida: Optional[str] = None,
    grafo=None,
) -> dict:
    """Ejecuta el flujo completo y devuelve el estado final. Si hay carpeta, guarda el informe."""
    if max_pasos is None:
        max_pasos = int(os.getenv("L10N_QA_MAX_STEPS", MAX_PASOS_POR_DEFECTO))
    estado = crear_estado_inicial(solicitud, origen, destino, max_pasos)
    final = (grafo or graph).invoke(estado, config={"recursion_limit": 50})
    if carpeta_salida:
        final["rutas_informe"] = guardar_informe(final["final_report"], carpeta_salida)
    return final


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="L10n QA Orchestrator: QA estructural de JSON traducidos.")
    parser.add_argument("--origen", nargs="+", default=[str(RAIZ / "fixtures" / "source")],
                        help="Archivos o carpetas de origen (por defecto, fixtures/source).")
    parser.add_argument("--destino", nargs="+", default=[str(RAIZ / "fixtures" / "targets")],
                        help="Archivos o carpetas de destino (por defecto, fixtures/targets).")
    parser.add_argument("--solicitud", default=SOLICITUD_EJEMPLO, help="Solicitud en lenguaje natural.")
    parser.add_argument("--max-pasos", type=int, default=None, help="Máximo de ejecuciones de nodos (por defecto 12).")
    parser.add_argument("--salida", default=os.getenv("L10N_QA_SALIDA", "salida"), help="Carpeta del informe.")
    args = parser.parse_args(argv)

    final = ejecutar_qa(args.solicitud, args.origen, args.destino, args.max_pasos, args.salida)
    print(informe_a_markdown(final["final_report"]))
    print(f"Informe guardado en: {final['rutas_informe']['json']} y {final['rutas_informe']['markdown']}")
    return CODIGOS_SALIDA[final["final_report"]["estado_general"]]


if __name__ == "__main__":
    sys.exit(main())
