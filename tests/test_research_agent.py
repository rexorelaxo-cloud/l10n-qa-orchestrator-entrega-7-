"""Pruebas del Agente de Investigación y de sus herramientas."""
import hashlib
from pathlib import Path

from app.agents.research_agent import contexto_investigacion, research_node
from conftest import DESTINO, ORIGEN, aplicar
from app.state import crear_estado_inicial, entradas_investigacion, reglas_recuperadas
from app.tools.file_inspector import cargar_json, inspect_localization_files, separar_nombre
from app.tools.l10n_rules import (
    CONFIG_POR_DEFECTO, buscar_reglas, excepciones_aplicables, inferir_controles, search_l10n_qa_rules,
)


def test_inspeccion_real_de_fixtures_encuentra_pares_e_idiomas():
    r = inspect_localization_files.invoke({"source_paths": [ORIGEN], "target_paths": [DESTINO]})
    assert len(r["archivos"]) == 5
    assert {(p["base"], p["idioma_destino"]) for p in r["pares"]} == {("home", "es"), ("home", "ar"), ("clean", "es")}
    assert r["idiomas"] == {"origen": ["en"], "destino": ["ar", "es"]}
    assert all(a["json_valido"] and len(a["sha256"]) == 64 for a in r["archivos"])
    assert r["sin_par"] == [] and r["no_analizables"] == []


def test_inspeccion_detecta_json_invalido_inexistente_y_vacio(escribir_json, tmp_path):
    roto = escribir_json("roto.en.json", '{"a": 1,', crudo=True)
    vacio = escribir_json("vacio.en.json", "   ", crudo=True)
    r = inspect_localization_files.invoke(
        {"source_paths": [roto, vacio, str(tmp_path / "no_existe.en.json")], "target_paths": []})
    por_nombre = {Path(a["ruta"]).name: a for a in r["archivos"]}
    assert por_nombre["roto.en.json"]["error"]["tipo"] == "json_invalido"
    assert por_nombre["roto.en.json"]["error"]["linea"] == 1
    assert por_nombre["vacio.en.json"]["error"]["tipo"] == "vacio"
    assert por_nombre["no_existe.en.json"]["existe"] is False
    assert {n["motivo"] for n in r["no_analizables"]} == {"vacio", "inexistente"}


def test_inspeccion_destino_sin_origen_queda_sin_par(escribir_json):
    destino = escribir_json("huerfano.es.json", {"a": "b"})
    r = inspect_localization_files.invoke({"source_paths": [ORIGEN], "target_paths": [destino]})
    assert [s["rol"] for s in r["sin_par"] if s["ruta"] == destino] == ["destino"]


def test_separar_nombre():
    assert separar_nombre("x/home.es.json") == ("home", "es")
    assert separar_nombre("x/home.pt_BR.json") == ("home", "pt-BR")
    assert separar_nombre("x/home.json") == ("home", None)


def test_busqueda_recupera_reglas_relevantes_de_la_base_de_conocimiento():
    ids = lambda q, **kw: [r["id"] for r in search_l10n_qa_rules.invoke({"query": q, **kw})]
    assert ids("placeholders alterados en el destino")[0] == "REG-PH-001"
    assert ids("saltos de línea y caracteres escapados")[0] == "REG-NL-001"
    assert ids("metadatos de importación al CMS")[0] == "REG-MET-001"
    assert "REG-TIP-001" in ids("diferencias de tipo de dato")


def test_busqueda_por_categoria_y_orden_por_puntaje():
    r = buscar_reglas("excepciones", categorias=["excepciones"], top_k=10)
    assert {x["categoria"] for x in r} == {"excepciones"} and len(r) == 3
    assert [x["puntaje"] for x in r] == sorted((x["puntaje"] for x in r), reverse=True)
    sin_puntaje = buscar_reglas("zzz inexistente", categorias=["tipos_datos"])
    assert sin_puntaje and all(x["categoria"] == "tipos_datos" for x in sin_puntaje)


def test_la_base_incluye_reglas_para_todas_las_categorias_exigidas():
    cats = {r["categoria"] for r in buscar_reglas("a", categorias=None, top_k=100)} | \
           {r["categoria"] for c in ("correspondencia_claves", "tipos_datos", "estructuras_anidadas", "placeholders",
                                     "saltos_linea", "metadatos", "excepciones", "diferencias_legitimas")
            for r in buscar_reglas(c, categorias=[c])}
    assert cats >= {"correspondencia_claves", "tipos_datos", "estructuras_anidadas", "placeholders",
                    "saltos_linea", "metadatos", "excepciones", "diferencias_legitimas"}


def test_inferir_controles():
    assert inferir_controles("revisá solo los placeholders") == ["placeholders"]
    assert set(inferir_controles("claves faltantes y saltos de línea")) == {"claves_faltantes", "saltos_linea"}
    assert len(inferir_controles("hacé QA")) == 7  # sin pistas: todos los controles


def test_excepciones_aplicables_solo_si_la_clave_existe():
    assert [e["id"] for e in excepciones_aplicables(CONFIG_POR_DEFECTO, {"_gpiInternalData-rootPath", "x"})] == ["EXC-002"]
    assert excepciones_aplicables(CONFIG_POR_DEFECTO, {"otra"}) == []


def test_research_node_produce_investigacion_estructurada_sin_analizar(estado_base):
    huellas = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(ORIGEN).glob("*.json")}
    update = research_node(estado_base)
    est = aplicar(estado_base, update)

    assert {e["tipo"] for e in update["research_findings"]} == {"inspeccion", "controles", "reglas", "excepciones"}
    assert all(e["status"] == "confirmed" and e["evidencia"] for e in update["research_findings"])
    assert [e["id"] for e in update["research_findings"]] == ["R-001", "R-002", "R-003", "R-004"]
    assert "analysis_findings" not in update                      # no hace el análisis estructural
    assert len(reglas_recuperadas(est)) == 5
    ex = entradas_investigacion(est, "excepciones")[0]["datos"]["aplicables"]
    assert {e["id"] for e in ex} == {"EXC-001", "EXC-002"}
    assert huellas == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(ORIGEN).glob("*.json")}


def test_research_node_registra_contribucion(estado_base):
    update = research_node(estado_base)
    c = update["agent_contributions"][0]
    assert (c["agente"], c["tipo_analisis"], c["paso"]) == ("research", "investigacion_inicial", 1)
    assert c["hallazgos"] == ["R-001", "R-002", "R-003", "R-004"]
    assert "inspect_localization_files" in c["evidencias"] and "search_l10n_qa_rules" in c["evidencias"]
    assert c["estado_hallazgos"] == {"confirmed": 4, "rejected": 0, "pending": 0}
    assert update["step_count"] == 1 and update["messages"][0].name == "research"


def test_research_atiende_refinamiento_de_reglas_y_lo_limpia(estado_base):
    est = aplicar(estado_base, research_node(estado_base))
    est["pending_refinement"] = {"destino": "research", "tipo": "buscar_reglas", "origen": "supervisor",
                                 "motivo": "Faltan reglas", "detalle": {"categorias": ["tipos_datos", "metadatos"]}, "clave": "k"}
    update = research_node(est)
    assert update["pending_refinement"] is None
    nuevas = {r["categoria"] for r in update["research_findings"][0]["datos"]["reglas"]}
    assert nuevas == {"tipos_datos", "metadatos"}
    assert update["agent_contributions"][0]["atendio_refinamiento"]["tipo"] == "buscar_reglas"


def test_aislamiento_de_contexto_de_investigacion(estado_analizado):
    ctx = contexto_investigacion(estado_analizado)
    assert set(ctx) == {"instrucciones", "solicitud", "rutas_origen", "rutas_destino", "config", "refinamiento",
                        "ya_inspeccionado", "paso", "siguiente_id"}
    assert "messages" not in ctx and "analysis_findings" not in ctx and "agent_contributions" not in ctx
