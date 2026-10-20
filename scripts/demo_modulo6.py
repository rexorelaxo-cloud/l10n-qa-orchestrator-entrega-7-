"""Demostración del flujo completo: solicitud del usuario → delegación → informe final.

Uso:
    python demo.py
"""
import json
from pathlib import Path

from app.graph import guardar_diagrama_png, obtener_mermaid
from app.cli import RAIZ, SOLICITUD_EJEMPLO, ejecutar_qa
from app.report import informe_a_markdown
from app.state import ESTADO_HALLAZGO_ES


def titulo(texto: str) -> None:
    print("\n" + "=" * 78 + f"\n{texto}\n" + "=" * 78)


def mostrar_demo(carpeta_salida: str = "salida") -> dict:
    origen, destino = [str(RAIZ / "fixtures" / "source")], [str(RAIZ / "fixtures" / "targets")]

    titulo("1. SOLICITUD INICIAL")
    print(SOLICITUD_EJEMPLO)

    final = ejecutar_qa(SOLICITUD_EJEMPLO, origen, destino, carpeta_salida=carpeta_salida)

    titulo("2. DECISIONES DE ENRUTAMIENTO DEL SUPERVISOR")
    for d in final["routing_log"]:
        marca = " (refinamiento)" if d["refinamiento"] else ""
        print(f"  paso {d['paso']:>2} → {d['ruta']:<10}{marca}\n          {d['motivo']}")

    titulo("3. CONTRIBUCIÓN DEL AGENTE DE INVESTIGACIÓN")
    for c in [c for c in final["agent_contributions"] if c["agente"] == "research"]:
        print(f"  paso {c['paso']:>2} [{c['tipo_analisis']}] {c['resumen']}")
    reglas = {}
    for e in final["research_findings"]:
        if e["tipo"] == "reglas":
            reglas.update({r["id"]: r["titulo"] for r in e["datos"]["reglas"]})
    print("  Reglas recuperadas:")
    for rid, t in sorted(reglas.items()):
        print(f"    - {rid}: {t}")

    titulo("4. HALLAZGOS DEL AGENTE DE ANÁLISIS")
    for c in [c for c in final["agent_contributions"] if c["agente"] == "analysis"]:
        print(f"  paso {c['paso']:>2} [{c['tipo_analisis']}] {c['resumen']}")
    for h in final["analysis_findings"]:
        print(f"  {h['id']} [{h['severidad']:<7}] {Path(h['archivo']).name:<14} {h['ruta_json']:<28} "
              f"{h['categoria']} ({ESTADO_HALLAZGO_ES[h['estado']]})")

    titulo("5. VALIDACIÓN FINAL")
    for v in final["validation_results"]["verificaciones"]:
        print(f"  [{'OK' if v['ok'] else 'FALLA'}] {v['descripcion']} — {v['detalle']}")

    titulo("6. INFORME DE QA (resumen)")
    informe = final["final_report"]
    print(f"  Estado general: {informe['estado_general']} ({informe['estado_general_es']})")
    print(f"  {informe['descripcion_estado']}")
    print(f"  Resumen: {informe['resumen']}")
    print(f"  Informe completo: {final['rutas_informe']['markdown']}")

    titulo("7. ESTADO DE FINALIZACIÓN")
    print(f"  task_completed = {final['task_completed']} · pasos = {final['step_count']}/{final['max_steps']} "
          f"· agentes = {', '.join(informe['agentes_contribuyentes'])}")

    carpeta = Path(carpeta_salida)
    (carpeta / "grafo.mmd").write_text(obtener_mermaid(), encoding="utf-8")
    return final


if __name__ == "__main__":
    final = mostrar_demo()
    png = guardar_diagrama_png("docs/grafo.png")
    print(f"\nDiagrama Mermaid guardado en salida/grafo.mmd. PNG generado: {'sí' if png else 'no (requiere red o dependencias opcionales)'}.")
