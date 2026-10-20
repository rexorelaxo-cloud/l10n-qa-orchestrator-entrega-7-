"""Pruebas del Agente de Análisis y de las herramientas de validación estructural."""
import hashlib
from pathlib import Path

import pytest

from app.agents.analyst_agent import analysis_node, contexto_analisis
from conftest import FIXTURES, aplicar, firmas, firmas_esperadas
from app.state import datos_analisis
from app.tools.file_inspector import cargar_json
from app.tools.l10n_rules import CONFIG_POR_DEFECTO
from app.tools.structural_validator import (
    check_placeholders_and_newlines, compare_json_structure, comparar_estructura,
    comparar_placeholders_y_saltos, contar_saltos, extraer_placeholders, validar_reglas_y_excepciones,
    validate_rules_and_exceptions, verificar_evidencia,
)


def cats(hallazgos):
    return {(h["categoria"], h["ruta_json"]) for h in hallazgos}


# --- Claves, tipos y estructuras -----------------------------------------------------------
def test_claves_faltantes_e_inesperadas_en_objetos_anidados():
    o = {"a": {"b": 1, "c": {"d": "x"}}, "e": "y"}
    d = {"a": {"b": 1, "c": {}, "extra": 2}, "z": 1}
    h = comparar_estructura(o, d)
    assert cats(h) == {("clave_faltante", "a.c.d"), ("clave_faltante", "e"),
                       ("clave_inesperada", "a.extra"), ("clave_inesperada", "z")}
    assert all(x["severidad"] == "error" for x in h)


def test_diferencia_de_tipos_y_bool_no_es_number():
    h = comparar_estructura({"n": 3, "b": True, "s": "x", "z": None, "l": []},
                            {"n": "3", "b": 1, "s": ["x"], "z": "", "l": {}})
    tipos = {x["ruta_json"]: (x["evidencia"]["tipo_origen"], x["evidencia"]["tipo_destino"]) for x in h}
    assert tipos == {"n": ("number", "string"), "b": ("boolean", "number"), "s": ("string", "array"),
                     "z": ("null", "string"), "l": ("array", "object")}


def test_arrays_se_comparan_por_indice_y_longitud():
    h = comparar_estructura({"l": [{"a": 1}, {"a": 2}]}, {"l": [{"a": 1}]})
    assert cats(h) == {("longitud_array_distinta", "l")}
    h2 = comparar_estructura({"l": [{"a": 1}, {"a": 2}]}, {"l": [{"a": 1}, {}]})
    assert cats(h2) == {("clave_faltante", "l[1].a")}


def test_valor_vacio_es_advertencia_y_no_se_completa():
    h = comparar_estructura({"t": "Hola"}, {"t": ""})
    assert [(x["categoria"], x["severidad"]) for x in h] == [("valor_vacio", "warning")]


def test_los_controles_limitan_lo_que_se_reporta():
    o, d = {"a": 1, "b": "x"}, {"a": "1", "c": 1}
    assert cats(comparar_estructura(o, d, ["tipos_datos"])) == {("tipo_distinto", "a")}
    assert cats(comparar_estructura(o, d, ["claves_faltantes"])) == {("clave_faltante", "b")}


# --- Placeholders ---------------------------------------------------------------------------
@pytest.mark.parametrize("texto,esperado", [
    ("Hola {name}", ["{name}"]), ("Hasta {{fecha}}", ["{{fecha}}"]), ("Total: ${precio}", ["${precio}"]),
    ("Llamar al %s y %d veces, %1$s", ["%s", "%d", "%1$s"]), ("Sin marcadores. 50% de descuento", []),
])
def test_extraccion_de_placeholders(texto, esperado):
    assert extraer_placeholders(texto) == esperado


def test_placeholder_renombrado_faltante_y_sobrante():
    h = comparar_placeholders_y_saltos({"a": "Hi {n}", "b": "{x} y {y}", "c": "ok"},
                                       {"a": "Hola {nombre}", "b": "{x}", "c": "ok {extra}"})
    assert cats(h) == {("placeholder_alterado", "a"), ("placeholder_alterado", "b"), ("placeholder_alterado", "c")}
    a = next(x for x in h if x["ruta_json"] == "a")["evidencia"]
    assert a["faltantes_en_destino"] == ["{n}"] and a["sobrantes_en_destino"] == ["{nombre}"]


def test_el_orden_de_los_placeholders_puede_variar():
    assert comparar_placeholders_y_saltos({"a": "{x} luego {y}"}, {"a": "{y} قبل {x}"}) == []


def test_llaves_dobles_no_equivalen_a_llaves_simples():
    h = comparar_placeholders_y_saltos({"a": "{{n}}"}, {"a": "{n}"})
    assert cats(h) == {("placeholder_alterado", "a")}


# --- Saltos de línea -------------------------------------------------------------------------
def test_distingue_salto_real_de_secuencia_literal():
    assert contar_saltos("a\nb") == {"reales": 1, "literales": 0}
    assert contar_saltos("a\\nb") == {"reales": 0, "literales": 1}
    assert contar_saltos("a\r\nb") == {"reales": 1, "literales": 0}


def test_literal_en_lugar_de_real_es_error_y_viceversa():
    h = comparar_placeholders_y_saltos({"a": "x\ny", "b": "x\\ny"}, {"a": "x\\ny", "b": "x\ny"})
    assert {(x["ruta_json"], x["categoria"], x["severidad"]) for x in h} == {
        ("a", "salto_linea_tipo_distinto", "error"), ("b", "salto_linea_tipo_distinto", "error")}


def test_cantidad_distinta_de_saltos_es_advertencia_y_misma_cantidad_no_reporta():
    h = comparar_placeholders_y_saltos({"a": "x\ny\nz", "b": "x\ny"}, {"a": "x y z", "b": "p\nq"})
    assert [(x["ruta_json"], x["severidad"]) for x in h] == [("a", "warning")]


# --- Metadatos y excepciones ----------------------------------------------------------------
def test_excepciones_de_metadata_se_documentan_y_no_son_error():
    o = {"_gpiInternalData-toolName": "A", "_gpiInternalData-rootPath": "/en/x"}
    d = {"_gpiInternalData-toolName": "B", "_gpiInternalData-rootPath": "/es/x"}
    h, aplicadas = validar_reglas_y_excepciones(o, d, config=CONFIG_POR_DEFECTO)
    assert {(x["categoria"], x["severidad"]) for x in h} == {("metadato_diferencia_permitida", "info")}
    assert {a["id"] for a in aplicadas} == {"EXC-001", "EXC-002"}
    assert all(a["justificacion"] for a in aplicadas)


def test_no_se_ignoran_todos_los_metadatos():
    o = {"_gpiInternalData-itemId": "1", "_gpiInternalData-otra": "a", "_gpiInternalData-rootPath": "/en"}
    d = {"_gpiInternalData-itemId": "2", "_gpiInternalData-otra": "b", "_gpiInternalData-rootPath": "/es"}
    h, aplicadas = validar_reglas_y_excepciones(o, d, config=CONFIG_POR_DEFECTO)
    errores = {x["ruta_json"] for x in h if x["severidad"] == "error"}
    assert errores == {"_gpiInternalData-itemId", "_gpiInternalData-otra"}
    assert [a["id"] for a in aplicadas] == ["EXC-002"]


def test_excepcion_aplica_tambien_a_metadatos_anidados():
    h, aplicadas = validar_reglas_y_excepciones({"x": {"_gpiInternalData-toolName": "a"}},
                                                {"x": {"_gpiInternalData-toolName": "b"}}, config=CONFIG_POR_DEFECTO)
    assert aplicadas and aplicadas[0]["ruta_json"] == "x._gpiInternalData-toolName"


def test_metadato_igual_no_genera_hallazgo():
    h, aplicadas = validar_reglas_y_excepciones({"_gpiInternalData-itemId": "1"}, {"_gpiInternalData-itemId": "1"})
    assert h == [] and aplicadas == []


# --- Diferencias legítimas ---------------------------------------------------------------------
def test_texto_igual_al_origen_y_longitud_distinta_no_son_errores():
    o = {"marca": "Acme", "titulo": "Welcome", "url": "/home", "tel": "+1 555 0100", "ok": "Same size"}
    d = {"marca": "Acme", "titulo": "Bienvenidos a nuestra tienda en línea", "url": "/home", "tel": "+1 555 0100", "ok": "Same size"}
    todos = comparar_estructura(o, d) + comparar_placeholders_y_saltos(o, d)
    assert todos == []
    h, _ = validar_reglas_y_excepciones(o, d)
    assert len(h) == 1 and h[0]["severidad"] == "info" and h[0]["categoria"] == "diferencia_legitima"
    ev = h[0]["evidencia"]
    assert set(ev["valores_identicos_al_origen"]) == {"marca", "ok"}      # URL y teléfono no cuentan
    assert ev["longitud_notablemente_distinta"] == ["titulo"]


# --- Herramientas LangChain sobre archivos reales -----------------------------------------------
def test_herramientas_procesan_archivos_reales():
    o, d = str(FIXTURES / "source" / "home.en.json"), str(FIXTURES / "targets" / "home.es.json")
    est = compare_json_structure.invoke({"source_path": o, "target_path": d})
    assert cats(est["hallazgos"]) == {("clave_faltante", "page.description"), ("clave_inesperada", "page.promoBadge"),
                                      ("tipo_distinto", "page.itemCount")}
    ph = check_placeholders_and_newlines.invoke({"source_path": o, "target_path": d})
    assert cats(ph["hallazgos"]) == {("placeholder_alterado", "page.banner.heading"),
                                     ("salto_linea_tipo_distinto", "page.banner.legal")}
    reg = validate_rules_and_exceptions.invoke({"source_path": o, "target_path": d, "config": CONFIG_POR_DEFECTO})
    assert [a["id"] for a in reg["excepciones_aplicadas"]] == ["EXC-002"]


def test_herramientas_informan_error_de_entrada_sin_lanzar_excepcion(escribir_json, tmp_path):
    ok = escribir_json("a.en.json", {"a": 1})
    roto = escribir_json("b.es.json", "{", crudo=True)
    r = compare_json_structure.invoke({"source_path": ok, "target_path": roto})
    assert r["hallazgos"] == [] and r["error_entrada"]["destino"]["tipo"] == "json_invalido"
    r = check_placeholders_and_newlines.invoke({"source_path": ok, "target_path": str(tmp_path / "no.json")})
    assert r["error_entrada"]["destino"]["tipo"] == "inexistente"


# --- Agente de Análisis -------------------------------------------------------------------------
@pytest.mark.parametrize("archivo,esperado", [
    ("home.es.json", "home_es.expected.json"), ("home.ar.json", "home_ar.expected.json"),
    ("clean.es.json", "clean_es.expected.json")])
def test_analisis_coincide_con_resultados_esperados(estado_analizado, archivo, esperado):
    assert firmas(estado_analizado["analysis_findings"], archivo) == firmas_esperadas(esperado)


def test_hallazgos_estructurados_con_evidencia_y_estado(estado_analizado):
    for h in estado_analizado["analysis_findings"]:
        assert {"id", "severidad", "categoria", "regla", "archivo", "archivo_origen", "ruta_json",
                "mensaje", "evidencia", "estado", "agente"} <= set(h)
        assert h["evidencia"] and h["agente"] == "analysis"
        assert h["estado"] == {"error": "confirmed", "warning": "pending", "info": "confirmed"}[h["severidad"]]
        ok, detalle = verificar_evidencia(h)
        assert ok, f"{h['id']}: {detalle}"


def test_resultados_reproducibles(estado_investigado):
    from app.agents.supervisor_agent import supervisor_node
    est = aplicar(estado_investigado, supervisor_node(estado_investigado))
    a, b = analysis_node(est)["analysis_findings"], analysis_node(est)["analysis_findings"]
    assert a == b and len(a) == 15


def test_el_analisis_no_modifica_ningun_archivo(estado_investigado):
    archivos = sorted((FIXTURES).rglob("*.json"))
    antes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in archivos}
    from app.agents.supervisor_agent import supervisor_node
    est = aplicar(estado_investigado, supervisor_node(estado_investigado))
    update = analysis_node(est)
    assert antes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in archivos}
    integ = update["agent_contributions"][0]["datos"]["integridad"]
    assert len(integ) == 5 and all(v["sha256_antes"] == v["sha256_despues"] for v in integ.values())


def test_el_destino_no_se_completa_con_el_origen(estado_analizado):
    es, _ = cargar_json(str(FIXTURES / "targets" / "home.es.json"))
    assert "description" not in es["page"] and es["page"]["itemCount"] == "3"
    assert es["page"]["banner"]["heading"] == "¡Hola, {nombreUsuario}!"      # texto traducido intacto


def test_contribucion_del_analisis_queda_registrada(estado_analizado):
    c = [c for c in estado_analizado["agent_contributions"] if c["agente"] == "analysis"][0]
    assert c["tipo_analisis"] == "analisis_estructural" and len(c["hallazgos"]) == 15
    assert c["estado_hallazgos"] == {"confirmed": 13, "rejected": 0, "pending": 2}
    assert {"compare_json_structure", "check_placeholders_and_newlines", "validate_rules_and_exceptions"} <= set(c["evidencias"])
    assert len(c["datos"]["pares_comparados"]) == 3 and len(c["datos"]["excepciones_aplicadas"]) == 4


def test_aislamiento_de_contexto_de_analisis(estado_investigado):
    from app.agents.supervisor_agent import supervisor_node
    est = aplicar(estado_investigado, supervisor_node(estado_investigado))
    ctx = contexto_analisis(est)
    assert set(ctx) == {"instrucciones", "solicitud", "pares", "sin_par", "controles_a_ejecutar", "controles_sin_regla",
                        "reglas", "config", "refinamiento", "hallazgos_previos", "paso"}
    assert "messages" not in ctx and "research_findings" not in ctx and "agent_contributions" not in ctx
    assert {e["id"] for e in ctx["config"]["excepciones"]} == {"EXC-001", "EXC-002"}
    assert all(set(r) == {"id", "categoria", "titulo"} for r in ctx["reglas"])


def _estado_con(escribir_json, origen, destino, solicitud="Revisá claves faltantes y tipos de datos"):
    from conftest import avanzar_hasta
    from app.state import crear_estado_inicial
    est = avanzar_hasta(crear_estado_inicial(solicitud, [origen], [destino]), "analysis")
    from app.agents.supervisor_agent import supervisor_node
    return aplicar(est, supervisor_node(est))


def test_json_invalido_en_destino_es_error_confirmado(escribir_json):
    o = escribir_json("a.en.json", {"a": 1})
    d = escribir_json("a.es.json", '{"a": ', crudo=True)
    upd = analysis_node(_estado_con(escribir_json, o, d))
    h = upd["analysis_findings"][0]
    assert (h["severidad"], h["categoria"], h["estado"]) == ("error", "json_invalido", "confirmed")
    assert h["evidencia"]["error"]["tipo"] == "json_invalido"
    assert verificar_evidencia(h)[0] is True


def test_origen_invalido_o_inexistente_no_es_analizable(escribir_json, tmp_path):
    o = escribir_json("a.en.json", "{", crudo=True)
    d = escribir_json("a.es.json", {"a": 1})
    upd = analysis_node(_estado_con(escribir_json, o, d))
    datos = upd["agent_contributions"][0]["datos"]
    assert [n["motivo"] for n in datos["no_analizables"]] == ["json_invalido"]
    assert datos["pares_comparados"] == []
    assert upd["analysis_findings"][0]["categoria"] == "json_invalido"


def test_verificar_evidencia_detecta_hallazgos_alterados(estado_analizado):
    h = dict(next(x for x in estado_analizado["analysis_findings"] if x["categoria"] == "tipo_distinto"))
    h["evidencia"] = {**h["evidencia"], "valor_destino": "otro"}
    assert verificar_evidencia(h)[0] is False
    assert verificar_evidencia({**h, "evidencia": {}})[0] is False
