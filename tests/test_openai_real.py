"""Pruebas con llamadas REALES a OpenAI. Se omiten salvo que se pida con ``-m openai_real`` y haya credenciales.

    OPENAI_API_KEY=... OPENAI_MODEL=... LLM_PROVIDER=openai pytest -m openai_real -v

Consumen una cantidad mínima de tokens (un resumen corto por ejecución).
"""
import os

import pytest
from conftest import DESTINO, ORIGEN

from app.checkpointer import RedisCheckpointSaver
from app.config import Settings
from app.graph import construir_grafo
from app.llm.factory import crear_llm
from app.state import crear_estado_inicial

pytestmark = [pytest.mark.openai_real, pytest.mark.redis]


@pytest.fixture
def settings_openai(redis_url, tmp_path):
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")):
        pytest.skip("Faltan OPENAI_API_KEY y/o OPENAI_MODEL: no se hace ninguna llamada real.")
    from conftest import hacer_settings
    return hacer_settings(redis_url, tmp_path, LLM_PROVIDER="openai", OPENAI_API_KEY=os.environ["OPENAI_API_KEY"],
                          OPENAI_MODEL=os.environ["OPENAI_MODEL"],
                          OPENAI_PRICE_INPUT_PER_1M=os.getenv("OPENAI_PRICE_INPUT_PER_1M", ""),
                          OPENAI_PRICE_OUTPUT_PER_1M=os.getenv("OPENAI_PRICE_OUTPUT_PER_1M", ""))


async def test_openai_real_registra_modelo_y_tokens(settings_openai, clientes):
    handle = crear_llm(settings_openai)
    grafo = construir_grafo(checkpointer=RedisCheckpointSaver(async_client=clientes[1], prefix=settings_openai.redis_prefix),
                            settings=settings_openai, llm=handle)
    estado = crear_estado_inicial("QA estructural", [f"{ORIGEN}/home.en.json"], [f"{DESTINO}/home.es.json"], 12,
                                  job_id="real1", thread_id="real1")
    final = await grafo.ainvoke(estado, {"configurable": {"thread_id": "real1"}})
    r = final["resumen_llm"]
    assert r["llamada_real"] is True and r["proveedor"] == "openai" and r["texto"]
    assert r["uso"]["informado"] and r["uso"]["tokens_entrada"] > 0 and r["uso"]["tokens_salida"] > 0
    print("\nUSO REAL:", r["modelo"], r["uso"], r["costo"])
