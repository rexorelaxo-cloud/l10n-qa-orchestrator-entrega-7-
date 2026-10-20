"""Flujos completos: API → Redis → cola → worker → LangGraph → HITL → resultado (modo local, Redis real)."""
import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from conftest import CUERPO_OK, FIXTURES, cliente_api, esperar_http

pytestmark = pytest.mark.redis
DECISION = {"decision": "approve", "approver": "diego.farias", "comment": "Revisado"}


def firmas():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (FIXTURES).rglob("*.json")}


@pytest.fixture
def s_embebido(settings):
    return replace(settings, embedded_worker=True)


async def test_flujo_completo_con_aprobacion(s_embebido):
    antes = firmas()
    async with cliente_api(s_embebido) as c:
        r = await c.post("/tasks", json=CUERPO_OK)
        assert r.status_code == 202
        job = r.json()["job_id"]
        t = await esperar_http(c, job, {"WAITING_APPROVAL"})
        assert t["approval_request"]["paquete"]["estado_qa"] == "PASS" and t["result"] is None
        assert not (s_embebido.output_dir / "importaciones" / f"{job}.json").exists()      # aún bloqueada

        a = await c.post(f"/tasks/{job}/approve", json=DECISION)
        assert a.status_code == 202 and a.json()["decision"] == "approve"
        dup = await c.post(f"/tasks/{job}/approve", json=DECISION)
        assert dup.status_code == 409                                                        # aprobación duplicada

        t = await esperar_http(c, job, {"DONE", "FAILED", "REJECTED"})
        assert t["status"] == "DONE", t
        assert t["approval"]["approver"] == "diego.farias" and t["approval"]["decided_at"]
        assert t["result"]["importacion"]["estado"] == "EJECUTADA" and t["result"]["estado_general"] == "PASS"
        manifiesto = json.loads((s_embebido.output_dir / "importaciones" / f"{job}.json").read_text(encoding="utf-8"))
        assert manifiesto["simulada"] is True and manifiesto["aprobacion"]["approver"] == "diego.farias"
        assert t["result"]["resumen_llm"]["llamada_real"] is False           # modo local: nada simulado como LLM
        assert t["metrics"]["execution_s"] > 0 and t["metrics"]["stages"]
        # la resolución ya quedó cerrada
        assert (await c.post(f"/tasks/{job}/approve", json=DECISION)).status_code == 409
    assert firmas() == antes                                      # las traducciones no se modificaron


async def test_flujo_con_rechazo_no_ejecuta_la_operacion_critica(s_embebido):
    async with cliente_api(s_embebido) as c:
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        await esperar_http(c, job, {"WAITING_APPROVAL"})
        r = await c.post(f"/tasks/{job}/approve", json={"decision": "reject", "approver": "ana", "comment": "no"})
        assert r.status_code == 202
        t = await esperar_http(c, job, {"DONE", "FAILED", "REJECTED"})
        assert t["status"] == "REJECTED" and t["approval"]["decision"] == "reject" and t["approval"]["comment"] == "no"
        assert t["result"]["importacion"]["estado"] == "RECHAZADA"
        assert not (s_embebido.output_dir / "importaciones" / f"{job}.json").exists()


async def test_hallazgos_de_un_archivo_con_errores_coinciden_con_lo_esperado(s_embebido):
    esperado = json.loads((FIXTURES / "expected_results" / "home_es.expected.json").read_text(encoding="utf-8"))
    cuerpo = {**CUERPO_OK, "source_paths": ["source/home.en.json"], "target_paths": ["targets/home.es.json"]}
    async with cliente_api(s_embebido) as c:
        job = (await c.post("/tasks", json=cuerpo)).json()["job_id"]
        t = await esperar_http(c, job, {"DONE", "FAILED", "WAITING_APPROVAL"})
        # El paquete con errores no es elegible: no se pide aprobación y la tarea termina.
        assert t["status"] == "DONE" and t["result"]["estado_general"] == esperado["estado_general"] == "FAIL"
        encontrados = {(h["categoria"], h["ruta_json"]) for h in t["result"]["hallazgos"] if h["severidad"] == "error"}
        assert {(h["categoria"], h["ruta_json"]) for h in esperado["hallazgos"] if h["severidad"] == "error"} <= encontrados
        assert all(h["archivo"] and h["ruta_json"] and h["mensaje"] for h in t["result"]["hallazgos"])
        assert t["result"]["importacion"]["estado"] == "NO_ELEGIBLE"


async def test_dos_aprobaciones_simultaneas_solo_una_es_aceptada(s_embebido):
    async with cliente_api(s_embebido) as c:
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        await esperar_http(c, job, {"WAITING_APPROVAL"})
        respuestas = await asyncio.gather(*[
            c.post(f"/tasks/{job}/approve", json={"decision": "approve", "approver": f"u{i}"}) for i in range(8)])
        assert sorted(r.status_code for r in respuestas) == [202] + [409] * 7
        t = await esperar_http(c, job, {"DONE", "FAILED", "REJECTED"})
        assert t["status"] == "DONE" and t["attempts"] == 2          # 1 ejecución inicial + 1 reanudación
        assert len(list((s_embebido.output_dir / "importaciones").iterdir())) == 1


async def test_cinco_solicitudes_concurrentes_quedan_aisladas(s_embebido):
    pares = [("clean", "clean"), ("home", "home")] * 3
    async with cliente_api(s_embebido) as c:
        cuerpos = [{**CUERPO_OK, "source_paths": [f"source/{n}.en.json"],
                    "target_paths": [f"targets/{n}.{'es' if i % 2 == 0 else 'ar'}.json"]}
                   for i, (n, _) in enumerate(pares[:5])]
        respuestas = await asyncio.gather(*[c.post("/tasks", json=b) for b in cuerpos])
        assert [r.status_code for r in respuestas] == [202] * 5
        ids = [r.json()["job_id"] for r in respuestas]
        assert len(set(ids)) == 5 and len({r.json()["thread_id"] for r in respuestas}) == 5
        finales = await asyncio.gather(*[esperar_http(c, j, {"WAITING_APPROVAL", "DONE", "FAILED"}) for j in ids])
        for i, t in enumerate(finales):
            assert t["status"] in {"WAITING_APPROVAL", "DONE"}, t
            assert t["job_id"] == ids[i]
        # clean (índices 0, 2, 4) pasa y espera aprobación; home (1, 3) termina con hallazgos
        assert [t["status"] for t in finales] == ["WAITING_APPROVAL", "DONE", "WAITING_APPROVAL", "DONE", "WAITING_APPROVAL"]
        assert finales[1]["result"]["estado_general"] == "FAIL" and finales[3]["result"]["estado_general"] == "FAIL"


async def test_la_tarea_pendiente_de_aprobacion_sobrevive_al_reinicio_de_api_y_worker(s_embebido):
    # Proceso 1: crea la tarea y llega a WAITING_APPROVAL; luego se apaga por completo.
    async with cliente_api(s_embebido) as c1:
        job = (await c1.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        await esperar_http(c1, job, {"WAITING_APPROVAL"})
    # Proceso 2: API, worker, clientes y grafo nuevos sobre el mismo Redis.
    async with cliente_api(s_embebido) as c2:
        t = (await c2.get(f"/tasks/{job}")).json()
        assert t["status"] == "WAITING_APPROVAL" and t["approval_request"]
        assert (await c2.post(f"/tasks/{job}/approve", json=DECISION)).status_code == 202
        t = await esperar_http(c2, job, {"DONE", "FAILED", "REJECTED"})
        assert t["status"] == "DONE" and t["result"]["importacion"]["estado"] == "EJECUTADA"


async def test_tarea_encolada_antes_de_que_exista_el_worker_se_procesa_despues(settings):
    async with cliente_api(settings) as c:                       # API sin worker
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        await asyncio.sleep(0.2)
        assert (await c.get(f"/tasks/{job}")).json()["status"] == "PENDING"
    async with cliente_api(replace(settings, embedded_worker=True)) as c2:     # llega un worker
        t = await esperar_http(c2, job, {"WAITING_APPROVAL"})
        assert t["attempts"] == 1
