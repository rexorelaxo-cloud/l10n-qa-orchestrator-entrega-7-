"""Human-in-the-Loop: interrupción real del grafo, aprobación, rechazo y reanudación tras reiniciar."""
import json
from pathlib import Path

import pytest
from conftest import DESTINO, ORIGEN
from langgraph.types import Command

from app.checkpointer import RedisCheckpointSaver
from app.graph import construir_grafo
from app.hitl import crear_nodo_importacion, nodo_puerta_aprobacion
from app.state import crear_estado_inicial
from app.worker import crear_clientes_redis

pytestmark = pytest.mark.redis
CFG = {"configurable": {"thread_id": "hilo-h"}}
APRUEBA = {"decision": "approve", "approver": "ana", "comment": "ok", "decided_at": "2026-01-01T00:00:00+00:00"}
RECHAZA = {**APRUEBA, "decision": "reject"}


def estado(origen="clean.en.json", destino="clean.es.json"):
    return crear_estado_inicial("QA estructural", [f"{ORIGEN}/{origen}"], [f"{DESTINO}/{destino}"], 12,
                                job_id="jobh1", thread_id="hilo-h", require_approval=True)


def grafo_de(settings, binario):
    return construir_grafo(checkpointer=RedisCheckpointSaver(async_client=binario, prefix=settings.redis_prefix),
                           settings=settings)


def manifiesto(settings) -> Path:
    return settings.output_dir / "importaciones" / "jobh1.json"


async def test_interrupcion_real_antes_de_la_operacion_critica(settings, clientes):
    grafo = grafo_de(settings, clientes[1])
    res = await grafo.ainvoke(estado(), CFG)
    assert "__interrupt__" in res and res["__interrupt__"][0].value["tipo"] == "aprobacion_importacion_cms"
    snap = await grafo.aget_state(CFG)
    assert snap.next == ("approval_gate",)                    # el grafo está DETENIDO en la puerta
    assert snap.values["import_status"] != "EJECUTADA"
    assert not manifiesto(settings).exists()                   # la operación crítica no corrió
    # el checkpoint está en Redis
    assert await clientes[0].keys(f"{settings.redis_prefix}:ckpt:hilo-h:*")


async def test_aprobacion_reanuda_el_mismo_grafo_y_ejecuta_la_operacion(settings, clientes):
    grafo = grafo_de(settings, clientes[1])
    await grafo.ainvoke(estado(), CFG)
    final = await grafo.ainvoke(Command(resume=APRUEBA), CFG)
    assert final["import_status"] == "EJECUTADA" and final["approval"]["approver"] == "ana"
    datos = json.loads(manifiesto(settings).read_text(encoding="utf-8"))
    assert datos["simulada"] is True and datos["aprobacion"]["decision"] == "approve"
    assert (await grafo.aget_state(CFG)).next == ()


async def test_rechazo_no_ejecuta_la_operacion_critica(settings, clientes):
    grafo = grafo_de(settings, clientes[1])
    await grafo.ainvoke(estado(), CFG)
    final = await grafo.ainvoke(Command(resume=RECHAZA), CFG)
    assert final["import_status"] == "RECHAZADA"
    assert not manifiesto(settings).exists()
    assert "execute_import" not in [t["nodo"] for t in final["stage_timings"]]


@pytest.mark.parametrize("respuesta", ["yes", {"decision": "tal vez"}, {"approver": "x"}, 1])
async def test_una_respuesta_invalida_nunca_cuenta_como_aprobacion(settings, clientes, respuesta):
    grafo = grafo_de(settings, clientes[1])
    await grafo.ainvoke(estado(), CFG)
    final = await grafo.ainvoke(Command(resume=respuesta), CFG)
    assert final["import_status"] == "RECHAZADA" and not manifiesto(settings).exists()


async def test_un_paquete_con_errores_no_es_elegible_y_no_pide_aprobacion(settings, clientes):
    grafo = grafo_de(settings, clientes[1])
    final = await grafo.ainvoke(estado("home.en.json", "home.es.json"), CFG)
    assert "__interrupt__" not in final and final["import_status"] == "NO_ELEGIBLE"
    assert final["final_report"]["estado_general"] == "FAIL" and not manifiesto(settings).exists()


async def test_flujo_sin_aprobacion_requerida(settings, clientes):
    e = estado(); e["require_approval"] = False
    final = await grafo_de(settings, clientes[1]).ainvoke(e, CFG)
    assert final["import_status"] == "NO_SOLICITADA" and not manifiesto(settings).exists()


async def test_la_operacion_critica_se_niega_sin_aprobacion_valida(settings):
    nodo = crear_nodo_importacion(settings.output_dir)
    for st in ({}, {"import_status": "RECHAZADA"}, {"import_status": "APROBADA", "approval": {"decision": "reject"}},
               {"import_status": "NO_ELEGIBLE"}):
        with pytest.raises(RuntimeError):
            nodo({**st, "job_id": "jobh1"})
    assert not manifiesto(settings).exists()


async def test_la_importacion_es_idempotente(settings):
    nodo = crear_nodo_importacion(settings.output_dir)
    st = {"import_status": "APROBADA", "approval": APRUEBA, "job_id": "jobh1", "thread_id": "t", "final_report": {}}
    primero, segundo = nodo(st), nodo(st)
    assert primero["import_result"]["ya_existia"] is False and segundo["import_result"]["ya_existia"] is True
    assert len(list((settings.output_dir / "importaciones").iterdir())) == 1


def test_job_id_inseguro_se_rechaza(settings):
    nodo = crear_nodo_importacion(settings.output_dir)
    with pytest.raises(ValueError):
        nodo({"import_status": "APROBADA", "approval": APRUEBA, "job_id": "../../etc/passwd"})


async def test_el_flujo_se_recupera_despues_de_reiniciar_el_proceso(settings, clientes):
    g1 = grafo_de(settings, clientes[1])
    await g1.ainvoke(estado(), CFG)
    # Reinicio: nuevos clientes, checkpointer y grafo. Solo Redis conserva el estado.
    texto2, bin2 = crear_clientes_redis(settings)
    try:
        g2 = grafo_de(settings, bin2)
        assert (await g2.aget_state(CFG)).next == ("approval_gate",)
        final = await g2.ainvoke(Command(resume=APRUEBA), CFG)
        assert final["import_status"] == "EJECUTADA" and manifiesto(settings).exists()
    finally:
        await texto2.aclose(); await bin2.aclose()


async def test_reanudar_dos_veces_no_ejecuta_dos_veces_la_operacion(settings, clientes):
    grafo = grafo_de(settings, clientes[1])
    await grafo.ainvoke(estado(), CFG)
    await grafo.ainvoke(Command(resume=APRUEBA), CFG)
    ctime = manifiesto(settings).stat().st_mtime_ns
    snap = await grafo.aget_state(CFG)
    assert snap.next == ()                                    # el grafo ya terminó: no hay nada que reanudar
    assert manifiesto(settings).stat().st_mtime_ns == ctime
