"""Pruebas del grafo, del enrutamiento del Supervisor, de los refinamientos y del límite de pasos."""
import pytest

from app.agents.analyst_agent import analysis_node
from app.agents.supervisor_agent import decidir_siguiente, enrutar_supervisor, supervisor_node
from conftest import DESTINO, FIXTURES, ORIGEN, aplicar, avanzar_hasta
from app.graph import construir_grafo, enrutar_tras_especialista, graph, obtener_mermaid
from app.cli import SOLICITUD_EJEMPLO, ejecutar_qa
from app.state import L10nState, MessagesState, crear_estado_inicial


def test_el_estado_hereda_de_messages_state():
    assert "messages" in L10nState.__annotations__
    assert MessagesState in L10nState.__orig_bases__          # herencia declarada (TypedDict no admite issubclass)
    assert L10nState.__annotations__["messages"] == MessagesState.__annotations__["messages"]
    for campo in ("user_request", "source_files", "target_files", "research_findings", "analysis_findings",
                  "validation_results", "agent_contributions", "next_agent", "task_completed", "step_count",
                  "max_steps", "errors", "final_report"):
        assert campo in L10nState.__annotations__, campo


def test_topologia_del_grafo_compilado():
    g = graph.get_graph()
    assert {"supervisor", "research", "analysis", "validation"} <= set(g.nodes)
    aristas = {(e.source, e.target) for e in g.edges}
    assert ("__start__", "supervisor") in aristas
    for esp in ("research", "analysis"):
        assert ("supervisor", esp) in aristas and (esp, "supervisor") in aristas   # los especialistas vuelven al supervisor
    assert ("supervisor", "validation") in aristas and ("validation", "supervisor") in aristas
    # Módulo 7: la validación ya no termina el grafo; pasa por el resumen, la aprobación humana y la importación.
    assert ("validation", "llm_report") in aristas and ("llm_report", "approval_gate") in aristas
    assert ("approval_gate", "execute_import") in aristas and ("approval_gate", "__end__") in aristas
    assert ("execute_import", "__end__") in aristas
    assert all(e.conditional for e in g.edges if e.source == "supervisor" and e.target != "supervisor")


def test_mermaid_disponible_sin_dependencias_opcionales():
    m = obtener_mermaid()
    assert m.startswith("---") or "graph TD" in m
    for nodo in ("supervisor", "research", "analysis", "validation"):
        assert nodo in m


def test_primer_enrutamiento_es_investigacion(estado_base):
    d = decidir_siguiente(estado_base)
    assert d["ruta"] == "research" and "Falta evidencia" in d["motivo"]
    upd = supervisor_node(estado_base)
    assert upd["next_agent"] == "research" and enrutar_supervisor({**estado_base, **upd}) == "research"


def test_supervisor_decide_con_el_estado_no_con_una_secuencia_fija(estado_base):
    # Con la investigación hecha y reglas incompletas pide un refinamiento concreto a research.
    est = aplicar(estado_base, supervisor_node(estado_base))
    est = aplicar(est, __import__("app.agents.research_agent", fromlist=["x"]).research_node(est))
    d = decidir_siguiente(est)
    assert d["ruta"] == "research" and d["refinamiento"]["tipo"] == "buscar_reglas"
    assert "tipos_datos" in d["refinamiento"]["detalle"]["categorias"]
    # Con reglas suficientes y sin análisis, enruta a análisis.
    assert decidir_siguiente(avanzar_hasta(estado_base, "analysis"))["ruta"] == "analysis"


def test_refinamiento_no_se_repite_cuando_ya_se_pidio(estado_base):
    est = avanzar_hasta(estado_base, "analysis")
    pedidos = [r for r in est["refinement_log"] if r["tipo"] == "buscar_reglas"]
    assert len(pedidos) == 1
    est = aplicar(est, supervisor_node(est)); est = aplicar(est, analysis_node(est))
    assert decidir_siguiente(est)["ruta"] == "validation"
    assert len([r for r in est["refinement_log"] if r["tipo"] == "buscar_reglas"]) == 1


def test_supervisor_pide_refinamiento_de_analisis_una_sola_vez(estado_investigado):
    est = aplicar(estado_investigado, supervisor_node(estado_investigado))
    parcial = analysis_node({**est, "pending_refinement": None})
    # Simulamos un análisis que solo ejecutó un control.
    parcial["agent_contributions"][0]["datos"]["controles_ejecutados"] = ["claves_faltantes"]
    est = aplicar(est, parcial)
    d = decidir_siguiente(est)
    assert d["ruta"] == "analysis" and d["refinamiento"]["tipo"] == "ejecutar_controles"
    assert "placeholders" in d["refinamiento"]["detalle"]["controles"]
    est = aplicar(est, supervisor_node(est))                    # registra el pedido
    assert est["pending_refinement"]["destino"] == "analysis"
    est = aplicar(est, analysis_node(est))                      # lo atiende y lo limpia
    assert est["pending_refinement"] is None
    assert decidir_siguiente(est)["ruta"] == "validation"       # no repite aunque falten controles ejecutados


def test_ejecucion_completa_enruta_dinamicamente_y_los_especialistas_vuelven_al_supervisor():
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])
    rutas = [d["ruta"] for d in final["routing_log"]]
    assert rutas == ["research", "research", "analysis", "validation"]
    agentes = [c["agente"] for c in final["agent_contributions"]]
    assert agentes == ["supervisor", "research", "supervisor", "research", "supervisor", "analysis", "supervisor", "validation"]
    assert final["step_count"] == 8 and final["task_completed"] is True and final["next_agent"] == "END"
    assert any(d["refinamiento"] for d in final["routing_log"])
    assert final["validation_results"]["verificaciones"] and final["final_report"]["estado_general"] == "FAIL"


def test_solicitud_acotada_enruta_distinto_y_ejecuta_solo_esos_controles():
    final = ejecutar_qa("Verificá únicamente los placeholders", [ORIGEN], [DESTINO])
    assert final["final_report"]["controles_ejecutados"] == ["placeholders"]
    assert {h["categoria"] for h in final["analysis_findings"] if h["severidad"] == "error"} == {"placeholder_alterado"}
    assert "refinamiento" not in "".join(str(d["refinamiento"]) for d in final["routing_log"] if d["paso"] == 1)


# --- Límite de pasos ------------------------------------------------------------------------
@pytest.mark.parametrize("max_pasos", [4, 5, 6])
def test_limite_de_pasos_cierra_como_incompleto(max_pasos):
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO], max_pasos=max_pasos)
    assert final["step_count"] <= max_pasos
    assert final["task_completed"] is True
    informe = final["final_report"]
    assert informe["estado_general"] == "INCOMPLETE" and informe["estado_general_es"] == "INCOMPLETO"
    assert any("análisis" in p.lower() or "controles" in p.lower() for p in informe["problemas_pendientes"])
    assert "PASS" != informe["estado_general"]


@pytest.mark.parametrize("max_pasos", [7, 8, 12, 20])
def test_con_pasos_suficientes_se_completa_sin_superar_el_limite(max_pasos):
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO], max_pasos=max_pasos)
    assert final["step_count"] <= max_pasos
    assert final["final_report"]["estado_general"] == "FAIL"


def test_max_pasos_demasiado_bajo_se_rechaza():
    with pytest.raises(ValueError, match="al menos"):
        crear_estado_inicial("x", [ORIGEN], [DESTINO], max_pasos=2)


def test_el_limite_por_defecto_es_12():
    assert crear_estado_inicial("x", [], [])["max_steps"] == 12


def test_ruta_posterior_al_especialista_reserva_el_ultimo_paso():
    assert enrutar_tras_especialista({"step_count": 5, "max_steps": 12}) == "supervisor"
    assert enrutar_tras_especialista({"step_count": 11, "max_steps": 12}) == "validation"


# --- Conflictos entre agentes -------------------------------------------------------------------
def _analisis_contradictorio(state):
    """Análisis real al que se le agrega una afirmación incompatible con la investigación."""
    upd = analysis_node(state)
    destino = str(FIXTURES / "targets" / "clean.es.json")
    upd["analysis_findings"].append({
        "id": "F-900", "severidad": "error", "categoria": "json_invalido", "regla": "REG-SINTAXIS",
        "archivo": destino, "archivo_origen": str(FIXTURES / "source" / "clean.en.json"), "idioma": "es",
        "ruta_json": "$", "mensaje": "Afirmación contradictoria inyectada para la prueba.",
        "evidencia": {"archivo_invalido": destino, "rol": "destino",
                      "error": {"tipo": "json_invalido", "mensaje": "Expecting value", "linea": 1, "columna": 1}},
        "estado": "confirmed", "excepcion_aplicada": None, "agente": "analysis"})
    upd["agent_contributions"][0]["hallazgos"].append("F-900")
    return upd


def test_conflicto_se_conserva_se_pide_investigacion_y_termina_en_revision_pendiente():
    grafo = construir_grafo(analysis=_analisis_contradictorio)
    origen, destino = [str(FIXTURES / "source" / "clean.en.json")], [str(FIXTURES / "targets" / "clean.es.json")]
    final = ejecutar_qa(SOLICITUD_EJEMPLO, origen, destino, grafo=grafo)

    assert final["step_count"] <= 12
    # 1) Se pidió una investigación específica a research (origen: validación).
    pedidos = [r for r in final["refinement_log"] if r["tipo"] == "verificar_archivos"]
    assert len(pedidos) == 1 and pedidos[0]["origen"] == "validation" and pedidos[0]["destino"] == "research"
    assert [d["ruta"] for d in final["routing_log"]][-2:] == ["research", "validation"]
    assert any(e["tipo"] == "reinspeccion" for e in final["research_findings"])
    # 2) Ambas contribuciones se conservan: nada se descartó silenciosamente.
    assert "F-900" in {h["id"] for h in final["analysis_findings"]}
    assert any(e["tipo"] == "inspeccion" and e["datos"]["archivos"][1]["json_valido"] for e in final["research_findings"])
    # 3) El conflicto sigue abierto y el resultado es revisión pendiente.
    abiertos = [c for c in final["final_report"]["conflictos"] if c["estado"] == "sin_resolver"]
    assert len(abiertos) == 1 and abiertos[0]["tipo"] == "json_valido_vs_invalido"
    informe = final["final_report"]
    assert informe["estado_general"] == "NEEDS_REVIEW"
    assert any("Conflicto sin resolver" in p for p in informe["problemas_pendientes"])
    rechazado = [h for lista in informe["hallazgos_por_severidad"].values() for h in lista if h["id"] == "F-900"][0]
    assert rechazado["estado"] == "rejected"


# --- Errores de entrada ---------------------------------------------------------------------------
def test_solicitud_vacia_es_fallo_controlado():
    final = ejecutar_qa("   ", [ORIGEN], [DESTINO])
    assert final["step_count"] == 2 and final["routing_log"][0]["ruta"] == "validation"
    assert final["final_report"]["estado_general"] == "INCOMPLETE"
    assert any("vacía" in p for p in final["final_report"]["problemas_pendientes"])


def test_sin_archivos_es_fallo_controlado():
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [], [])
    assert final["final_report"]["estado_general"] == "INCOMPLETE"
    assert [d["ruta"] for d in final["routing_log"]] == ["research", "validation"]
    assert any("ningún archivo" in p for p in final["final_report"]["problemas_pendientes"])


def test_archivo_inexistente_deja_el_informe_incompleto(tmp_path):
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [str(FIXTURES / "source" / "clean.en.json")], [str(tmp_path / "clean.es.json")])
    informe = final["final_report"]
    assert informe["estado_general"] == "INCOMPLETE"
    assert informe["archivos_no_analizados"][0]["motivo"] == "inexistente"
    assert informe["archivos_analizados"] == []


def test_json_invalido_en_destino_da_fail_y_no_incompleto(escribir_json):
    o = escribir_json("p.en.json", {"a": "x"})
    d = escribir_json("p.es.json", '{"a": }', crudo=True)
    final = ejecutar_qa("Revisá la sintaxis y las claves", [o], [d])
    assert final["final_report"]["estado_general"] == "FAIL"
    assert [h["categoria"] for h in final["analysis_findings"]] == ["json_invalido"]


def test_error_de_herramienta_se_registra_y_no_rompe_el_flujo(monkeypatch):
    import app.agents.analyst_agent as modulo
    def falla(*_a, **_k): raise RuntimeError("fallo simulado")
    class Falsa:
        invoke = staticmethod(falla)
    monkeypatch.setattr(modulo, "compare_json_structure", Falsa)
    final = ejecutar_qa(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])
    assert final["errors"] and "fallo simulado" in final["errors"][0]
    assert final["final_report"]["estado_general"] == "INCOMPLETE"
    assert any("Error de ejecución" in p for p in final["final_report"]["problemas_pendientes"])
