"""Pruebas del nodo de Validación, del cálculo de estados y del informe."""
import json
import shutil
from pathlib import Path

import pytest

from app.agents.validation_agent import detectar_conflictos, validation_node
from conftest import DESTINO, FIXTURES, ORIGEN, aplicar
from app.cli import SOLICITUD_EJEMPLO, ejecutar_qa
from app.report import (
    calcular_estado, conflictos_vigentes, conteo_por_severidad, guardar_informe, informe_a_markdown,
    verificar_fidelidad,
)
from app.state import ESTADO_GENERAL_ES, crear_estado_inicial

LIMPIO_O = str(FIXTURES / "source" / "clean.en.json")
LIMPIO_D = str(FIXTURES / "targets" / "clean.es.json")


def verificaciones(update):
    return {v["id"]: v for v in update["validation_results"]["verificaciones"]}


def test_validacion_sobre_ejecucion_correcta_aprueba_todas_las_verificaciones(estado_analizado):
    upd = validation_node(estado_analizado)
    v = verificaciones(upd)
    assert set(v) == {"contribuciones", "estructura", "evidencia", "traducciones_intactas", "limite", "fidelidad"}
    assert all(x["ok"] for x in v.values())
    assert upd["task_completed"] is True and upd["next_agent"] == "END"
    assert upd["final_report"]["estado_general"] == "FAIL"           # hay errores confirmados en los fixtures
    assert upd["agent_contributions"][0]["agente"] == "validation"


def test_validacion_rechaza_hallazgo_con_estructura_invalida_sin_descartarlo(estado_analizado):
    est = dict(estado_analizado)
    est["analysis_findings"] = list(est["analysis_findings"]) + [{"id": "F-777", "severidad": "grave", "categoria": "x"}]
    upd = validation_node(est)
    assert verificaciones(upd)["estructura"]["ok"] is False
    informe = upd["final_report"]
    assert [h["id"] for h in informe["hallazgos_invalidos"]] == ["F-777"]          # no se descarta en silencio
    assert informe["hallazgos_invalidos"][0]["estado"] == "rejected"
    assert any("F-777" in p for p in informe["problemas_pendientes"])
    assert informe["estado_general"] == "FAIL"                                      # los errores reales siguen vigentes
    assert verificaciones(upd)["fidelidad"]["ok"] is True
    assert "F-777" in informe_a_markdown(informe)


def test_validacion_rechaza_evidencia_no_reproducible(estado_analizado):
    est = dict(estado_analizado)
    hallazgos = [dict(h) for h in est["analysis_findings"]]
    objetivo = next(h for h in hallazgos if h["categoria"] == "clave_faltante")
    objetivo["evidencia"] = {**objetivo["evidencia"], "presente_en_destino": True}   # evidencia falsa
    est["analysis_findings"] = hallazgos
    upd = validation_node(est)
    assert verificaciones(upd)["evidencia"]["ok"] is False
    assert objetivo["id"] in verificaciones(upd)["evidencia"]["detalle"]


def _estado_limpio():
    from conftest import avanzar_hasta
    from app.agents.analyst_agent import analysis_node
    from app.agents.supervisor_agent import supervisor_node
    est = avanzar_hasta(crear_estado_inicial(SOLICITUD_EJEMPLO, [LIMPIO_O], [LIMPIO_D]), "analysis")
    est = aplicar(est, supervisor_node(est))
    return aplicar(est, analysis_node(est))


def test_caso_limpio_da_pass():
    upd = validation_node(_estado_limpio())
    informe = upd["final_report"]
    assert informe["estado_general"] == "PASS" and informe["estado_general_es"] == "APROBADO"
    assert informe["resumen"] == {"errores": 0, "advertencias": 0, "informacion": 2, "total": 2}
    assert informe["problemas_pendientes"] == []
    assert "no significa" in informe["aclaracion"].lower() and "lingüísticamente" in informe["aclaracion"]


def test_si_cambia_un_archivo_durante_la_ejecucion_no_se_aprueba(tmp_path):
    o, d = tmp_path / "c.en.json", tmp_path / "c.es.json"
    shutil.copy(LIMPIO_O, o); shutil.copy(LIMPIO_D, d)
    from conftest import avanzar_hasta
    from app.agents.analyst_agent import analysis_node
    from app.agents.supervisor_agent import supervisor_node
    est = avanzar_hasta(crear_estado_inicial(SOLICITUD_EJEMPLO, [str(o)], [str(d)]), "analysis")
    est = aplicar(est, supervisor_node(est)); est = aplicar(est, analysis_node(est))
    d.write_text(d.read_text(encoding="utf-8").replace("Contáctenos", "Contacto"), encoding="utf-8")   # alteración externa
    upd = validation_node(est)
    assert verificaciones(upd)["traducciones_intactas"]["ok"] is False
    assert upd["final_report"]["estado_general"] == "NEEDS_REVIEW"


def test_estados_generales_y_sus_equivalentes_en_espanol(escribir_json):
    assert ESTADO_GENERAL_ES == {"PASS": "APROBADO", "FAIL": "FALLIDO", "NEEDS_REVIEW": "REQUIERE REVISIÓN", "INCOMPLETE": "INCOMPLETO"}
    o = escribir_json("w.en.json", {"t": "Hello"})
    d = escribir_json("w.es.json", {"t": ""})
    revision = ejecutar_qa("Revisá tipos de datos", [o], [d])["final_report"]
    assert (revision["estado_general"], revision["resumen"]["advertencias"]) == ("NEEDS_REVIEW", 1)
    assert all(ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])["final_report"]["descripcion_estado"] for _ in range(1))


def test_severidades_diferenciadas_en_el_informe():
    informe = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])["final_report"]
    assert set(informe["hallazgos_por_severidad"]) == {"error", "warning", "info"}
    for sev, lista in informe["hallazgos_por_severidad"].items():
        assert all(h["severidad"] == sev for h in lista)
    assert informe["resumen"] == {"errores": 6, "advertencias": 2, "informacion": 7, "total": 15}


def test_excepciones_y_reglas_aplicadas_quedan_documentadas():
    informe = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])["final_report"]
    ids = {e["id"] for e in informe["excepciones_aplicadas"]}
    assert ids == {"EXC-001", "EXC-002"}
    assert all(e["justificacion"] and e["ruta_json"] and e["archivo"] for e in informe["excepciones_aplicadas"])
    reglas = {r["id"] for r in informe["reglas_aplicadas"]}
    assert {"REG-PH-001", "REG-NL-001", "REG-CLV-001", "REG-MET-001"} <= reglas
    assert set(informe["agentes_contribuyentes"]) == {"supervisor", "research", "analysis", "validation"}


def test_fidelidad_detecta_un_informe_que_no_coincide_con_los_hallazgos():
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])
    informe, hallazgos = json.loads(json.dumps(final["final_report"])), final["analysis_findings"]
    assert verificar_fidelidad(informe, hallazgos)[0] is True
    informe["resumen"]["errores"] += 1
    assert verificar_fidelidad(informe, hallazgos)[0] is False
    informe["resumen"]["errores"] -= 1
    informe["hallazgos_por_severidad"]["error"].pop()
    assert verificar_fidelidad(informe, hallazgos)[0] is False


def test_conteo_por_severidad():
    h = [{"severidad": "error"}, {"severidad": "error"}, {"severidad": "info"}]
    assert conteo_por_severidad(h) == {"errores": 2, "advertencias": 0, "informacion": 1, "total": 3}


def test_detectar_conflictos_entre_investigacion_y_analisis(estado_analizado):
    assert detectar_conflictos(estado_analizado) == []
    est = dict(estado_analizado)
    est["research_findings"] = [dict(e) for e in est["research_findings"]]
    insp = est["research_findings"][0]
    insp["datos"] = json.loads(json.dumps(insp["datos"]))
    insp["datos"]["archivos"][2]["json_valido"] = False
    insp["datos"]["archivos"][2]["error"] = {"tipo": "json_invalido", "mensaje": "x"}
    c = detectar_conflictos(est)
    assert len(c) == 1 and c[0]["tipo"] == "invalido_vs_comparado" and c[0]["estado"] == "sin_resolver"


def test_informe_markdown_y_guardado_en_disco(tmp_path):
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])
    md = informe_a_markdown(final["final_report"])
    for texto in ("QA estructural y validación de preparación para importación", "Estado general: **FAIL** (FALLIDO)",
                  "page.banner.heading", "EXC-001", "Decisiones del Supervisor"):
        assert texto in md
    rutas = guardar_informe(final["final_report"], str(tmp_path / "out"))
    assert json.loads(Path(rutas["json"]).read_text(encoding="utf-8"))["estado_general"] == "FAIL"
    assert Path(rutas["markdown"]).read_text(encoding="utf-8") == md
