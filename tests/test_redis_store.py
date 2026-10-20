"""Estado operativo, cola y checkpoints sobre un Redis REAL."""
import asyncio

import pytest
from conftest import crear_tarea_directa

from app.checkpointer import RedisCheckpointSaver
from app.graph import construir_grafo
from app.queue import TaskQueue, crear_mensaje
from app.redis_store import TaskStore, TransicionInvalida
from app.schemas import TaskStatus
from app.state import crear_estado_inicial
from conftest import DESTINO, ORIGEN, hacer_settings

pytestmark = pytest.mark.redis


async def test_el_estado_de_una_tarea_se_recupera_desde_redis(entorno, settings):
    job_id = await crear_tarea_directa(entorno)
    # Un store NUEVO con un cliente NUEVO (como tras reiniciar la API) ve la misma tarea.
    from app.worker import crear_clientes_redis
    texto2, bin2 = crear_clientes_redis(settings)
    try:
        t = await TaskStore(texto2, settings.redis_prefix).obtener(job_id)
        assert t["status"] == "PENDING" and t["job_id"] == job_id and t["thread_id"] == f"thread-{job_id}"
        assert t["payload"]["require_approval"] is True and t["attempts"] == 0
    finally:
        await texto2.aclose(); await bin2.aclose()


async def test_tarea_inexistente_devuelve_none(entorno):
    assert await entorno.store.obtener("0" * 32) is None


async def test_transicion_atomica_solo_una_gana_la_carrera(entorno):
    job_id = await crear_tarea_directa(entorno)
    await entorno.store.transicionar(job_id, [TaskStatus.PENDING], TaskStatus.RUNNING)
    await entorno.store.transicionar(job_id, [TaskStatus.RUNNING], TaskStatus.WAITING_APPROVAL)
    resultados = await asyncio.gather(*[
        entorno.store.transicionar(job_id, [TaskStatus.WAITING_APPROVAL], TaskStatus.PENDING,
                                   encolar=crear_mensaje(job_id, "resume"), approval={"decision": "approve", "n": i})
        for i in range(10)])
    assert resultados.count(True) == 1
    # y se encoló exactamente UN mensaje de reanudación (además del inicial de la creación)
    assert await entorno.queue.largo_pendientes() == 2


async def test_transiciones_invalidas_se_rechazan(entorno):
    job_id = await crear_tarea_directa(entorno)
    with pytest.raises(TransicionInvalida):
        await entorno.store.transicionar(job_id, [TaskStatus.PENDING], TaskStatus.DONE)
    with pytest.raises(TransicionInvalida):
        await entorno.store.transicionar(job_id, [TaskStatus.DONE], TaskStatus.RUNNING)
    # una transición con estado de origen equivocado no modifica nada
    assert await entorno.store.transicionar(job_id, [TaskStatus.RUNNING], TaskStatus.FAILED) is False
    assert (await entorno.store.obtener(job_id))["status"] == "PENDING"


async def test_aislamiento_entre_tareas(entorno):
    a, b = await crear_tarea_directa(entorno), await crear_tarea_directa(entorno, "source/home.en.json", "targets/home.es.json")
    await entorno.store.transicionar(a, [TaskStatus.PENDING], TaskStatus.RUNNING, attempts=1)
    ta, tb = await entorno.store.obtener(a), await entorno.store.obtener(b)
    assert ta["status"] == "RUNNING" and tb["status"] == "PENDING"
    assert ta["thread_id"] != tb["thread_id"] and ta["payload"] != tb["payload"]


async def test_cola_fifo_reclamar_y_confirmar(entorno):
    q = entorno.queue
    r1, r2 = await q.encolar("a" * 32), await q.encolar("b" * 32)
    assert await q.reclamar() == r1
    assert [raw for raw, _ in await q.en_proceso()] == [r1]
    await q.confirmar(r1)
    assert await q.en_proceso() == []
    assert await q.reclamar() == r2
    assert await q.reclamar() is None


async def test_los_checkpoints_estan_separados_por_thread_id(settings, clientes):
    _, binario = clientes
    saver = RedisCheckpointSaver(async_client=binario, prefix=settings.redis_prefix)
    grafo = construir_grafo(checkpointer=saver, settings=settings)
    estados = {}
    for tid, (o, d) in {"hilo-A": ("clean.en.json", "clean.es.json"), "hilo-B": ("home.en.json", "home.es.json")}.items():
        estado = crear_estado_inicial("QA estructural", [f"{ORIGEN}/{o}"], [f"{DESTINO}/{d}"], 12, job_id=tid, thread_id=tid)
        await grafo.ainvoke(estado, {"configurable": {"thread_id": tid}})
        estados[tid] = (await grafo.aget_state({"configurable": {"thread_id": tid}})).values
    assert estados["hilo-A"]["final_report"]["estado_general"] == "PASS"
    assert estados["hilo-B"]["final_report"]["estado_general"] == "FAIL"
    assert estados["hilo-A"]["job_id"] == "hilo-A" and estados["hilo-B"]["job_id"] == "hilo-B"
    # borrar un hilo no afecta al otro
    await saver.adelete_thread("hilo-A")
    assert not (await grafo.aget_state({"configurable": {"thread_id": "hilo-A"}})).values
    assert (await grafo.aget_state({"configurable": {"thread_id": "hilo-B"}})).values["job_id"] == "hilo-B"


async def test_los_checkpoints_viven_en_redis_y_no_en_memoria(settings, clientes):
    texto, binario = clientes
    grafo = construir_grafo(checkpointer=RedisCheckpointSaver(async_client=binario, prefix=settings.redis_prefix),
                            settings=settings)
    estado = crear_estado_inicial("QA", [f"{ORIGEN}/clean.en.json"], [f"{DESTINO}/clean.es.json"], 12,
                                  job_id="x", thread_id="x")
    await grafo.ainvoke(estado, {"configurable": {"thread_id": "x"}})
    claves = await texto.keys(f"{settings.redis_prefix}:ckpt:x:*")
    assert len(claves) >= 5          # un checkpoint por superpaso
