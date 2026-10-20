"""Observabilidad: LangSmith opcional, correlación por tarea, tiempos por etapa y tokens."""
import os

import pytest
from conftest import DESTINO, ORIGEN
from fakes import handle_falso

from app.checkpointer import RedisCheckpointSaver
from app.config import Settings
from app.graph import construir_grafo
from app.observability import config_ejecucion, configurar_langsmith, extraer_uso, medir_nodo, percentil
from app.state import crear_estado_inicial


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch):
    for v in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(v, raising=False)


def test_sin_credenciales_la_app_funciona_sin_trazas_externas():
    r = configurar_langsmith(Settings.from_env({"LANGSMITH_TRACING": "false"}))
    assert r["activo"] is False and os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_pedido_sin_clave_se_desactiva_y_lo_informa():
    r = configurar_langsmith(Settings.from_env({"LANGSMITH_TRACING": "true"}))
    assert r["activo"] is False and "LANGSMITH_API_KEY" in r["motivo"] and os.environ["LANGSMITH_TRACING"] == "false"


def test_con_clave_se_activa_y_configura_el_proyecto():
    s = Settings.from_env({"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "lsv2_clave_de_prueba",
                           "LANGSMITH_PROJECT": "mi-proyecto"})
    r = configurar_langsmith(s)
    assert r == {"activo": True, "proyecto": "mi-proyecto"}            # la respuesta no expone la clave
    assert os.environ["LANGSMITH_PROJECT"] == "mi-proyecto" and os.environ["LANGSMITH_TRACING"] == "true"
    assert "lsv2_clave_de_prueba" not in str(r)


def test_la_configuracion_de_ejecucion_correlaciona_con_job_y_thread():
    c = config_ejecucion("abc123", "thread-abc123", proveedor="openai", modelo="m", intento=2)
    assert c["configurable"] == {"thread_id": "thread-abc123"}
    assert c["metadata"]["job_id"] == "abc123" and c["metadata"]["thread_id"] == "thread-abc123"
    assert "job:abc123" in c["tags"] and c["run_name"] == "l10n-qa:abc123" and c["metadata"]["intento"] == 2
    assert "api_key" not in str(c).lower()


def test_medir_nodo_agrega_la_duracion_de_la_etapa():
    r = medir_nodo("research", lambda s: {"x": 1})({"step_count": 3})
    assert r["x"] == 1 and r["stage_timings"][0]["nodo"] == "research"
    assert r["stage_timings"][0]["agente"] == "Research Agent" and r["stage_timings"][0]["segundos"] >= 0


def test_extraer_uso():
    class M:  # noqa: D401
        usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert extraer_uso(M()) == {"informado": True, "tokens_entrada": 10, "tokens_salida": 5, "tokens_totales": 15}
    assert extraer_uso(object()) == {"informado": False}


def test_percentil_nearest_rank():
    assert percentil([1, 2, 3, 4, 5], 95) == 5 and percentil([5, 1, 3], 50) == 3 and percentil([], 95) is None
    assert percentil(list(range(1, 101)), 95) == 95


@pytest.mark.redis
async def test_las_etapas_y_los_tokens_quedan_registrados_en_el_estado(settings, clientes):
    grafo = construir_grafo(checkpointer=RedisCheckpointSaver(async_client=clientes[1], prefix=settings.redis_prefix),
                            settings=settings, llm=handle_falso())
    estado = crear_estado_inicial("QA", [f"{ORIGEN}/clean.en.json"], [f"{DESTINO}/clean.es.json"], 12,
                                  job_id="j", thread_id="t")
    final = await grafo.ainvoke(estado, config_ejecucion("j", "t"))
    assert final["resumen_llm"]["uso"]["tokens_totales"] == 150 and final["resumen_llm"]["modelo"] == "falso-1"
    agentes = {e["agente"] for e in final["stage_timings"]}
    assert {"Supervisor Agent", "Research Agent", "Analysis Agent", "Validation Node", "Report Agent (LLM)"} <= agentes
