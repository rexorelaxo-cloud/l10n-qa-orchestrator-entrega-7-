"""Fábrica de LLM: selección de proveedor, validación de configuración y modo local determinista."""
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import ChatFalso, handle_falso

from app.agents.report_agent import crear_nodo_redactor
from app.config import ConfigError, Settings
from app.llm import factory
from app.llm.factory import (ManejadorLLM, costo_estimado, crear_llm, proveedores_disponibles,
                             registrar_proveedor, validar_configuracion)

RAIZ = Path(__file__).resolve().parent.parent
INFORME = {"estado_general": "FAIL", "resumen": {"errores": 1, "advertencias": 0, "informacion": 0},
           "hallazgos_por_severidad": {"error": [{"id": "H1", "categoria": "clave_faltante", "archivo": "a.json",
                                                  "ruta_json": "x.y", "mensaje": "Falta la clave"}],
                                       "warning": [], "info": []}}


def cfg(**env) -> Settings:
    return Settings.from_env({k: str(v) for k, v in env.items()})


def test_openai_se_selecciona_por_configuracion():
    h = crear_llm(cfg(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-prueba-no-real", OPENAI_MODEL="modelo-x"))
    assert h.proveedor == "openai" and h.es_real and h.modelo == "modelo-x"
    assert type(h.modelo_chat).__name__ == "ChatOpenAI"          # se construye, pero no se hace ninguna llamada


@pytest.mark.parametrize("env,fragmento", [
    ({"LLM_PROVIDER": "openai", "OPENAI_MODEL": "m"}, "OPENAI_API_KEY"),
    ({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}, "OPENAI_MODEL"),
])
def test_configuracion_faltante_da_un_error_claro(env, fragmento):
    with pytest.raises(ConfigError, match=fragmento):
        validar_configuracion(cfg(**env))
    with pytest.raises(ConfigError, match=fragmento):
        crear_llm(cfg(**env))


def test_proveedor_desconocido_lista_los_disponibles():
    with pytest.raises(ConfigError, match="no está implementado") as exc:
        crear_llm(cfg(LLM_PROVIDER="inventado"))
    assert "local" in str(exc.value) and "openai" in str(exc.value)


def test_no_hay_cambio_silencioso_a_modo_local():
    # Con openai y sin clave NUNCA se devuelve un manejador local: falla explícitamente.
    with pytest.raises(ConfigError):
        crear_llm(cfg(LLM_PROVIDER="openai"))


def test_modo_local_no_tiene_modelo_ni_simula_llamadas():
    h = crear_llm(cfg(LLM_PROVIDER="local"))
    assert h.proveedor == "local" and h.es_real is False and h.modelo_chat is None


def test_modo_local_es_determinista():
    nodo = crear_nodo_redactor(crear_llm(cfg(LLM_PROVIDER="local")), cfg(LLM_PROVIDER="local"))
    a, b = nodo({"final_report": INFORME}), nodo({"final_report": INFORME})
    for r in (a, b):
        r["resumen_llm"].pop("duracion_s")
    assert a == b and a["resumen_llm"]["llamada_real"] is False and a["resumen_llm"]["uso"] == {"informado": False}


def test_se_puede_agregar_un_proveedor_sin_tocar_agentes_ni_grafo():
    class ProveedorDePrueba:
        nombre = "prueba"
        def validar(self, settings): return None
        def crear(self, settings): return handle_falso()
    registrar_proveedor(ProveedorDePrueba())
    try:
        assert "prueba" in proveedores_disponibles()
        h = crear_llm(cfg(LLM_PROVIDER="prueba"))
        assert h.es_real and h.proveedor == "falso"
    finally:
        factory._REGISTRO.pop("prueba")


def test_el_redactor_registra_modelo_y_tokens_del_proveedor():
    h = handle_falso()
    r = crear_nodo_redactor(h, cfg(LLM_PROVIDER="local"))({"final_report": INFORME, "job_id": "j", "thread_id": "t"})["resumen_llm"]
    assert r["llamada_real"] is True and r["modelo"] == "falso-1" and r["texto"] == "Resumen ejecutivo de prueba."
    assert r["uso"] == {"informado": True, "tokens_entrada": 120, "tokens_salida": 30, "tokens_totales": 150}
    assert r["costo"]["origen"] == "desconocido" and r["costo"]["usd"] is None      # sin tarifas: no se inventa
    assert len(h.modelo_chat.llamadas) == 1


def test_el_prompt_no_incluye_evidencias_ni_contenido_traducido():
    h = handle_falso()
    informe = {**INFORME}
    informe["hallazgos_por_severidad"] = {"error": [{**INFORME["hallazgos_por_severidad"]["error"][0],
                                                     "evidencia": {"valor_destino": "TEXTO-TRADUCIDO-SECRETO"}}],
                                          "warning": [], "info": []}
    crear_nodo_redactor(h, cfg(LLM_PROVIDER="local"))({"final_report": informe})
    prompt = " ".join(m.content for m in h.modelo_chat.llamadas[0])
    assert "TEXTO-TRADUCIDO-SECRETO" not in prompt and "x.y" in prompt


def test_costo_solo_con_tarifa_configurada():
    s0 = cfg()
    assert costo_estimado(s0, 1000, 500)["origen"] == "desconocido"
    s1 = cfg(OPENAI_PRICE_INPUT_PER_1M="2", OPENAI_PRICE_OUTPUT_PER_1M="8")
    c = costo_estimado(s1, 1_000_000, 500_000)
    assert c["origen"] == "estimado_con_tarifa_configurada" and c["usd"] == pytest.approx(6.0)
    assert costo_estimado(s1, None, None)["origen"] == "desconocido"


def test_errores_del_proveedor_se_propagan_sin_fallback():
    h = handle_falso(error=RuntimeError("proveedor caído"))
    with pytest.raises(RuntimeError, match="proveedor caído"):
        crear_nodo_redactor(h, cfg())({"final_report": INFORME})


@pytest.mark.parametrize("env", [{"OPENAI_PRICE_INPUT_PER_1M": "abc"}, {"OPENAI_PRICE_INPUT_PER_1M": "-1"},
                                 {"WORKER_CONCURRENCY": "0"}, {"MAX_ATTEMPTS": "x"}])
def test_variables_invalidas_se_rechazan(env):
    with pytest.raises(ConfigError):
        cfg(**env)


def test_no_hay_dependencia_de_anthropic():
    for archivo in list((RAIZ / "app").rglob("*.py")) + [RAIZ / "requirements.txt", RAIZ / ".env.example"]:
        texto = archivo.read_text(encoding="utf-8").lower()
        assert "anthropic" not in texto and "claude" not in texto, archivo
    codigo = ("import sys, app.main, app.worker, app.llm.factory;"
              "print(any(m.split('.')[0] in ('anthropic', 'langchain_anthropic') for m in sys.modules))")
    salida = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, capture_output=True, text=True,
                            env={"PATH": "", "PYTHONPATH": str(RAIZ), "LLM_PROVIDER": "local"})
    assert salida.stdout.strip() == "False", salida.stderr
