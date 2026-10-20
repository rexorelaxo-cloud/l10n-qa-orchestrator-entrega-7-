"""Ejecución del grafo con checkpoints REALES en Redis: enrutamiento, límite de pasos y reanudación."""
import pytest
from conftest import DESTINO, ORIGEN

from app.agents.analyst_agent import analysis_node
from app.agents.research_agent import research_node
from app.checkpointer import RedisCheckpointSaver
from app.graph import construir_grafo
from app.state import crear_estado_inicial
from app.worker import crear_clientes_redis

pytestmark = pytest.mark.redis
CFG = {"configurable": {"thread_id": "hilo-1"}}


def estado(max_pasos=12, destino="clean.es.json", origen="clean.en.json", aprobacion=False):
    return crear_estado_inicial("QA estructural de localización", [f"{ORIGEN}/{origen}"], [f"{DESTINO}/{destino}"],
                                max_pasos, job_id="j1", thread_id="hilo-1", require_approval=aprobacion)


def crear_grafo(settings, binario, **kw):
    return construir_grafo(checkpointer=RedisCheckpointSaver(async_client=binario, prefix=settings.redis_prefix),
                           settings=settings, **kw)


async def test_ejecucion_completa_del_grafo_con_checkpointer_redis(settings, clientes):
    grafo = crear_grafo(settings, clientes[1])
    final = await grafo.ainvoke(estado(), CFG)
    assert final["final_report"]["estado_general"] == "PASS"
    assert final["resumen_llm"]["llamada_real"] is False          # modo local: no se simula un LLM
    assert final["import_status"] == "NO_SOLICITADA"
    assert {t["nodo"] for t in final["stage_timings"]} >= {"supervisor", "research", "analysis", "validation", "llm_report"}


async def test_el_supervisor_enruta_segun_el_estado(settings, clientes):
    grafo = crear_grafo(settings, clientes[1])
    final = await grafo.ainvoke(estado(destino="home.es.json", origen="home.en.json"), CFG)
    rutas = [r["ruta"] for r in final["routing_log"]]
    assert rutas[0] == "research" and "analysis" in rutas and rutas[-1] == "validation"
    assert final["final_report"]["estado_general"] == "FAIL"


async def test_limite_de_pasos(settings, clientes):
    grafo = crear_grafo(settings, clientes[1])
    final = await grafo.ainvoke(estado(max_pasos=4, destino="home.es.json", origen="home.en.json"), CFG)
    assert final["step_count"] <= 4
    assert final["final_report"]["estado_general"] in {"INCOMPLETE", "FAIL", "NEEDS_REVIEW", "PASS"}
    assert final["task_completed"] is True       # cierre controlado, no un bucle


async def test_reanudacion_desde_checkpoint_tras_una_falla(settings, clientes):
    llamadas = {"research": 0, "analysis": 0}

    def research(s):
        llamadas["research"] += 1
        return research_node(s)

    def analysis(s):
        llamadas["analysis"] += 1
        if llamadas["analysis"] == 1:
            raise RuntimeError("caída simulada del proceso")
        return analysis_node(s)

    # Línea base: cuántas veces ejecuta cada nodo una corrida SIN fallas (el supervisor puede pedir refinamientos).
    base = {"research": 0, "analysis": 0}

    def research_base(s):
        base["research"] += 1
        return research_node(s)

    def analysis_base(s):
        base["analysis"] += 1
        return analysis_node(s)

    await crear_grafo(settings, clientes[1], research=research_base, analysis=analysis_base).ainvoke(
        estado(), {"configurable": {"thread_id": "hilo-base"}})

    grafo = crear_grafo(settings, clientes[1], research=research, analysis=analysis)
    with pytest.raises(RuntimeError):
        await grafo.ainvoke(estado(), CFG)
    parcial = await grafo.aget_state(CFG)
    assert parcial.next == ("analysis",) and parcial.values["research_findings"]      # el trabajo previo sobrevivió
    final = await grafo.ainvoke(None, CFG)                                              # continúa del checkpoint
    assert final["final_report"]["estado_general"] == "PASS"
    # La investigación NO se repitió: mismas ejecuciones que la línea base; solo el nodo caído se reintentó (+1).
    assert llamadas == {"research": base["research"], "analysis": base["analysis"] + 1}


async def test_el_checkpoint_sobrevive_a_un_proceso_nuevo(settings, clientes):
    llamadas = {"analysis": 0}

    def analysis(s):
        llamadas["analysis"] += 1
        if llamadas["analysis"] == 1:
            raise RuntimeError("caída")
        return analysis_node(s)

    g1 = crear_grafo(settings, clientes[1], analysis=analysis)
    with pytest.raises(RuntimeError):
        await g1.ainvoke(estado(), CFG)
    # «Proceso nuevo»: clientes, checkpointer y grafo completamente nuevos sobre el mismo Redis.
    texto2, bin2 = crear_clientes_redis(settings)
    try:
        g2 = crear_grafo(settings, bin2)
        assert (await g2.aget_state(CFG)).next == ("analysis",)
        final = await g2.ainvoke(None, CFG)
        assert final["final_report"]["estado_general"] == "PASS"
    finally:
        await texto2.aclose(); await bin2.aclose()
