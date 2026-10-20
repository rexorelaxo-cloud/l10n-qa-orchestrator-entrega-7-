"""Worker y cola sobre Redis REAL: procesamiento, errores, reintentos, recuperación y concurrencia."""
import asyncio
import threading
import time

import pytest
from conftest import crear_tarea_directa, esperar_estado, hacer_settings

from app.agents.analyst_agent import analysis_node
from app.checkpointer import RedisCheckpointSaver
from app.graph import construir_grafo
from app.queue import TaskQueue
from app.redis_store import TaskStore
from app.schemas import TaskStatus
from app.worker import FabricaGrafo, Worker, es_transitorio, sanitizar_error

pytestmark = pytest.mark.redis


def worker_con_grafo(e, **nodos):
    saver = RedisCheckpointSaver(async_client=e.binario, prefix=e.settings.redis_prefix)
    grafo = construir_grafo(checkpointer=saver, settings=e.settings, **nodos)
    return Worker(e.settings, e.store, e.queue, FabricaGrafo(e.settings, e.binario, grafo=grafo), worker_id="w-test")


async def test_encolado_y_procesamiento_hasta_la_espera_de_aprobacion(entorno):
    job = await crear_tarea_directa(entorno)
    await entorno.worker.procesar_hasta_vaciar()
    t = await entorno.store.obtener(job)
    assert t["status"] == "WAITING_APPROVAL" and t["attempts"] == 1
    assert t["approval_request"]["tipo"] == "aprobacion_importacion_cms" and t["lease_until"] is None
    assert await entorno.queue.en_proceso() == [] and await entorno.queue.largo_pendientes() == 0


async def test_tarea_sin_aprobacion_termina_en_done_con_resultado_y_metricas(entorno):
    job = await crear_tarea_directa(entorno, "source/home.en.json", "targets/home.es.json", aprobacion=False)
    await entorno.worker.procesar_hasta_vaciar()
    t = await entorno.store.obtener(job)
    assert t["status"] == "DONE" and t["result"]["estado_general"] == "FAIL" and t["result"]["hallazgos"]
    h = t["result"]["hallazgos"][0]
    assert {"id", "severidad", "archivo", "ruta_json", "mensaje", "evidencia"} <= set(h)
    assert t["metrics"]["execution_s"] > 0 and t["metrics"]["total_s"] >= t["metrics"]["execution_s"]
    assert {s["nodo"] for s in t["metrics"]["stages"]} >= {"research", "analysis", "validation"}
    assert t["result"]["agentes_y_etapas"] and t["result"]["importacion"]["estado"] == "NO_SOLICITADA"


async def test_una_excepcion_del_grafo_deja_la_tarea_en_failed(entorno):
    def analisis_roto(_):
        raise ValueError("fallo interno con sk-ABCDEFGHIJKLMNOP secreto")
    w = worker_con_grafo(entorno, analysis=analisis_roto)
    job = await crear_tarea_directa(entorno)
    await w.procesar_hasta_vaciar()
    t = await entorno.store.obtener(job)
    assert t["status"] == "FAILED" and "ValueError" in t["error"] and t["attempts"] == 1
    assert "sk-ABCDEFGHIJKLMNOP" not in t["error"] and "[oculto]" in t["error"]     # sin credenciales
    assert t["finished_ts"] and t["lease_until"] is None                              # no queda en RUNNING


async def test_falla_transitoria_se_reintenta_de_forma_acotada(entorno):
    entorno.settings = hacer_settings(entorno.settings.redis_url, entorno.settings.output_dir.parent,
                                      REDIS_PREFIX=entorno.settings.redis_prefix, MAX_ATTEMPTS=2)
    intentos = {"n": 0}

    def inestable(s):
        intentos["n"] += 1
        raise ConnectionError("red caída")
    w = worker_con_grafo(entorno, analysis=inestable)
    w.s = entorno.settings
    job = await crear_tarea_directa(entorno)
    await w.procesar_hasta_vaciar(max_espera=30)
    t = await entorno.store.obtener(job)
    assert t["status"] == "FAILED" and t["attempts"] == 2 and intentos["n"] == 2     # exactamente MAX_ATTEMPTS


async def test_falla_transitoria_que_se_resuelve_completa_la_tarea(entorno):
    intentos = {"n": 0}

    def una_vez(s):
        intentos["n"] += 1
        if intentos["n"] == 1:
            raise ConnectionError("blip de red")
        return analysis_node(s)
    w = worker_con_grafo(entorno, analysis=una_vez)
    job = await crear_tarea_directa(entorno, aprobacion=False)
    await w.procesar_hasta_vaciar(max_espera=30)
    t = await entorno.store.obtener(job)
    assert t["status"] == "DONE" and t["attempts"] == 2 and t["result"]["estado_general"] == "PASS"


async def test_error_de_configuracion_openai_sin_clave_falla_con_mensaje_claro(redis_url, tmp_path, clientes):
    s = hacer_settings(redis_url, tmp_path, LLM_PROVIDER="openai")
    texto, binario = clientes
    store, queue = TaskStore(texto, s.redis_prefix), TaskQueue(texto, s.redis_prefix)
    w = Worker(s, store, queue, FabricaGrafo(s, binario))
    from app.queue import crear_mensaje
    await store.crear_y_encolar("a" * 32, "thread-a", {"request": "x", "source_paths": [], "target_paths": []},
                                crear_mensaje("a" * 32, "run"))
    await w.procesar_hasta_vaciar()
    t = await store.obtener("a" * 32)
    assert t["status"] == "FAILED" and "OPENAI_API_KEY" in t["error"] and "configuración" in t["error"]


async def test_tarea_running_tras_una_caida_se_recupera_y_se_completa(entorno):
    job = await crear_tarea_directa(entorno, aprobacion=False)
    raw = await entorno.queue.reclamar()                                 # un worker la reclamó...
    await entorno.store.transicionar(job, [TaskStatus.PENDING], TaskStatus.RUNNING, attempts=1,
                                     lease_until=time.time() - 60, started_ts=time.time() - 61)   # ...y murió
    assert (await entorno.store.obtener(job))["status"] == "RUNNING"
    assert await entorno.worker.recuperar_huerfanas() == 1
    assert (await entorno.store.obtener(job))["status"] == "PENDING" and await entorno.queue.en_proceso() == []
    await entorno.worker.procesar_hasta_vaciar()
    t = await entorno.store.obtener(job)
    assert t["status"] == "DONE" and t["attempts"] == 2 and raw


async def test_una_tarea_con_reserva_vigente_no_se_recupera(entorno):
    job = await crear_tarea_directa(entorno, aprobacion=False)
    await entorno.queue.reclamar()
    await entorno.store.transicionar(job, [TaskStatus.PENDING], TaskStatus.RUNNING, attempts=1,
                                     lease_until=time.time() + 60)
    assert await entorno.worker.recuperar_huerfanas() == 0
    assert (await entorno.store.obtener(job))["status"] == "RUNNING"


async def test_tarea_interrumpida_que_agoto_los_intentos_pasa_a_failed(entorno):
    job = await crear_tarea_directa(entorno)
    await entorno.queue.reclamar()
    await entorno.store.transicionar(job, [TaskStatus.PENDING], TaskStatus.RUNNING,
                                     attempts=entorno.settings.max_attempts, lease_until=time.time() - 1)
    await entorno.worker.recuperar_huerfanas()
    t = await entorno.store.obtener(job)
    assert t["status"] == "FAILED" and "máximo de intentos" in t["error"]


async def test_mensaje_huerfano_de_una_tarea_ya_resuelta_se_descarta(entorno):
    job = await crear_tarea_directa(entorno)
    await entorno.queue.reclamar()
    await entorno.store.transicionar(job, [TaskStatus.PENDING], TaskStatus.FAILED, error="x")
    await entorno.worker.recuperar_huerfanas()
    assert await entorno.queue.en_proceso() == []


async def test_entrega_duplicada_no_ejecuta_dos_veces(entorno):
    job = await crear_tarea_directa(entorno)
    await entorno.queue.encolar(job, "run")                             # mensaje duplicado de la misma tarea
    await entorno.worker.procesar_hasta_vaciar()
    t = await entorno.store.obtener(job)
    assert t["status"] == "WAITING_APPROVAL" and t["attempts"] == 1     # el duplicado fue ignorado


async def test_limite_de_concurrencia_y_aislamiento_entre_tareas(redis_url, tmp_path, clientes):
    s = hacer_settings(redis_url, tmp_path, WORKER_CONCURRENCY=2)
    texto, binario = clientes
    store, queue = TaskStore(texto, s.redis_prefix), TaskQueue(texto, s.redis_prefix)
    actual, maximo, lock = 0, 0, threading.Lock()

    def lento(st):
        nonlocal actual, maximo
        with lock:
            actual += 1
            maximo = max(maximo, actual)
        time.sleep(0.25)
        with lock:
            actual -= 1
        return analysis_node(st)

    saver = RedisCheckpointSaver(async_client=binario, prefix=s.redis_prefix)
    grafo = construir_grafo(checkpointer=saver, settings=s, analysis=lento)
    w = Worker(s, store, queue, FabricaGrafo(s, binario, grafo=grafo))

    class E: pass   # noqa: E701
    e = E(); e.store, e.queue, e.settings = store, queue, s
    jobs = [await crear_tarea_directa(e, "source/home.en.json" if i % 2 else "source/clean.en.json",
                                      "targets/home.es.json" if i % 2 else "targets/clean.es.json", aprobacion=False)
            for i in range(5)]
    await w.procesar_hasta_vaciar(max_espera=60)
    tareas = [await store.obtener(j) for j in jobs]
    assert maximo <= 2 and maximo >= 1                                   # nunca más de WORKER_CONCURRENCY a la vez
    assert [t["status"] for t in tareas] == ["DONE"] * 5
    assert len({t["thread_id"] for t in tareas}) == 5
    assert [t["result"]["estado_general"] for t in tareas] == ["PASS", "FAIL", "PASS", "FAIL", "PASS"]


async def test_apagado_ordenado_deja_las_tareas_en_curso_recuperables(entorno):
    job = await crear_tarea_directa(entorno, aprobacion=False)
    entorno.worker.s = entorno.settings
    corre = asyncio.create_task(entorno.worker.run_forever())
    t = await esperar_estado(lambda: entorno.store.obtener(job), {"DONE"})
    corre.cancel()
    with pytest.raises(asyncio.CancelledError):
        await corre
    await entorno.worker.detener(gracia=1)
    assert t["status"] == "DONE"


def test_clasificacion_de_errores():
    assert es_transitorio(ConnectionError()) and es_transitorio(TimeoutError()) and not es_transitorio(ValueError())
    assert "[oculto]" in sanitizar_error(RuntimeError("clave sk-1234567890ABCDEF y Bearer abc.def"))
