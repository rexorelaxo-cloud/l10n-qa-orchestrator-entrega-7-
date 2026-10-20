"""Pruebas de integración: de la solicitud inicial al informe final."""
import hashlib
import json
from pathlib import Path

import pytest

from conftest import DESTINO, FIXTURES, ORIGEN, esperado, firmas
from app.cli import SOLICITUD_EJEMPLO, ejecutar_qa, main
from app.report import informe_a_markdown

LIMPIO = ([str(FIXTURES / "source" / "clean.en.json")], [str(FIXTURES / "targets" / "clean.es.json")])


def huellas():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURES.rglob("*.json")}


@pytest.fixture(scope="module")
def corrida_completa():
    return ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])


def test_flujo_completo_desde_la_solicitud_hasta_el_informe(corrida_completa):
    f = corrida_completa
    assert f["user_request"] == SOLICITUD_EJEMPLO and f["messages"][0].content == SOLICITUD_EJEMPLO
    assert f["task_completed"] is True and f["next_agent"] == "END"
    assert f["research_findings"] and f["analysis_findings"] and f["validation_results"] and f["final_report"]
    assert {m.name for m in f["messages"]} == {"usuario", "supervisor", "research", "analysis", "validation"}
    informe = f["final_report"]
    assert informe["estado_general"] == "FAIL"
    assert informe["idiomas_analizados"] == {"origen": ["en"], "destino": ["ar", "es"]}
    assert len(informe["archivos_analizados"]) == 3 and informe["archivos_no_analizados"] == []


@pytest.mark.parametrize("archivo,esp", [("home.es.json", "home_es.expected.json"),
                                         ("home.ar.json", "home_ar.expected.json"),
                                         ("clean.es.json", "clean_es.expected.json")])
def test_se_detectan_los_defectos_deliberados_de_los_fixtures(corrida_completa, archivo, esp):
    reales = [h for h in corrida_completa["analysis_findings"] if Path(h["archivo"]).name == archivo]
    exp = esperado(esp)
    assert firmas(reales) == {(h["severidad"], h["categoria"], h["ruta_json"]) for h in exp["hallazgos"]}
    conteo = {"errores": sum(h["severidad"] == "error" for h in reales),
              "advertencias": sum(h["severidad"] == "warning" for h in reales),
              "informacion": sum(h["severidad"] == "info" for h in reales)}
    assert conteo == exp["resumen"]


def test_todos_los_defectos_pedidos_estan_cubiertos(corrida_completa):
    categorias = {h["categoria"] for h in corrida_completa["analysis_findings"]}
    assert {"clave_faltante", "clave_inesperada", "tipo_distinto", "placeholder_alterado",
            "salto_linea_tipo_distinto", "metadato_diferencia_permitida", "diferencia_legitima"} <= categorias


def test_el_informe_coincide_con_los_hallazgos_reales(corrida_completa):
    informe, hallazgos = corrida_completa["final_report"], corrida_completa["analysis_findings"]
    en_informe = {h["id"]: h for lista in informe["hallazgos_por_severidad"].values() for h in lista}
    assert set(en_informe) == {h["id"] for h in hallazgos}
    assert all(en_informe[h["id"]]["ruta_json"] == h["ruta_json"] and en_informe[h["id"]]["archivo"] == h["archivo"]
               for h in hallazgos)
    assert informe["resumen"]["errores"] == sum(h["severidad"] == "error" for h in hallazgos) == 6
    assert informe["resumen"]["total"] == len(hallazgos)
    md = informe_a_markdown(informe)
    assert all(h["id"] in md for h in hallazgos)


def test_caso_limpio_pasa_los_controles():
    f = ejecutar_qa(SOLICITUD_EJEMPLO, *LIMPIO)
    assert f["final_report"]["estado_general"] == "PASS"
    assert not [h for h in f["analysis_findings"] if h["severidad"] in ("error", "warning")]
    assert f["final_report"]["problemas_pendientes"] == []


def test_la_ejecucion_no_modifica_fixtures_ni_traducciones():
    antes = huellas()
    ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])
    assert huellas() == antes
    es = json.loads((FIXTURES / "targets" / "home.es.json").read_text(encoding="utf-8"))
    assert es["page"]["title"] == "Bienvenido a la tienda en línea de Acme"
    assert es["page"]["banner"]["legal"] == "Oferta válida hasta {{endDate}}.\\nAplican condiciones."


def test_cada_agente_registra_su_contribucion(corrida_completa):
    por_agente = {}
    for c in corrida_completa["agent_contributions"]:
        por_agente.setdefault(c["agente"], []).append(c)
    assert {k: len(v) for k, v in por_agente.items()} == {"supervisor": 4, "research": 2, "analysis": 1, "validation": 1}
    pasos = [c["paso"] for c in corrida_completa["agent_contributions"]]
    assert pasos == list(range(1, 9))
    assert por_agente["analysis"][0]["estado_hallazgos"] == {"confirmed": 13, "rejected": 0, "pending": 2}


def test_la_cli_devuelve_codigo_de_salida_y_guarda_el_informe(tmp_path, capsys):
    codigo = main(["--origen", ORIGEN, "--destino", DESTINO, "--salida", str(tmp_path)])
    assert codigo == 1
    assert json.loads((tmp_path / "informe_qa.json").read_text(encoding="utf-8"))["estado_general"] == "FAIL"
    assert "Estado general: **FAIL** (FALLIDO)" in capsys.readouterr().out
    assert main(["--origen", LIMPIO[0][0], "--destino", LIMPIO[1][0], "--salida", str(tmp_path / "ok")]) == 0
    assert main(["--max-pasos", "4", "--salida", str(tmp_path / "inc")]) == 3


def test_demo_se_ejecuta_de_principio_a_fin(tmp_path, capsys):
    from scripts.demo_modulo6 import mostrar_demo
    final = mostrar_demo(str(tmp_path))
    salida = capsys.readouterr().out
    for seccion in ("1. SOLICITUD INICIAL", "2. DECISIONES DE ENRUTAMIENTO", "3. CONTRIBUCIÓN DEL AGENTE DE INVESTIGACIÓN",
                    "4. HALLAZGOS DEL AGENTE DE ANÁLISIS", "5. VALIDACIÓN FINAL", "6. INFORME DE QA", "7. ESTADO DE FINALIZACIÓN"):
        assert seccion in salida
    assert final["final_report"]["estado_general"] == "FAIL" and (tmp_path / "grafo.mmd").exists()


def test_el_notebook_se_ejecuta_sin_errores():
    nbformat = pytest.importorskip("nbformat")
    nbclient = pytest.importorskip("nbclient")
    ruta = FIXTURES.parent / "notebooks" / "l10n_qa_demo.ipynb"
    nb = nbformat.read(str(ruta), as_version=4)
    try:
        nbclient.NotebookClient(nb, timeout=120, kernel_name="python3",
                                resources={"metadata": {"path": str(ruta.parent)}}).execute()
    except nbclient.exceptions.CellExecutionError as exc:
        pytest.fail(f"El notebook falló: {exc}")
    salida = "".join(o.get("text", "") for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []))
    assert "FAIL" in salida and "PASS" in salida and "INCOMPLETE" in salida
