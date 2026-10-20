"""API REST (FastAPI) sobre Redis real."""
import pytest
from conftest import CUERPO_OK, cliente_api, esperar_http, hacer_settings

from app.agents.analyst_agent import analysis_node  # noqa: F401
from app.checkpointer import RedisCheckpointSaver
from app.graph import construir_grafo

pytestmark = pytest.mark.redis


async def test_crear_tarea_responde_202_con_identificadores_sin_esperar_el_analisis(settings):
    async with cliente_api(settings) as c:                 # SIN worker: la API no ejecuta nada por sí misma
        r = await c.post("/tasks", json=CUERPO_OK)
        assert r.status_code == 202
        cuerpo = r.json()
        assert len(cuerpo["job_id"]) == 32 and cuerpo["thread_id"] == f"thread-{cuerpo['job_id']}"
        assert cuerpo["status"] == "PENDING" and cuerpo["status_url"] == f"/tasks/{cuerpo['job_id']}"
        assert r.headers["location"] == cuerpo["status_url"] and cuerpo["enqueue_ms"] < 1000
        # persistido en Redis y encolado
        app = c.app_ref
        assert (await app.state.store.obtener(cuerpo["job_id"]))["status"] == "PENDING"
        assert await app.state.queue.largo_pendientes() == 1


async def test_consultar_una_tarea_existente_lee_desde_redis(settings):
    async with cliente_api(settings) as c:
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        r = await c.get(f"/tasks/{job}")
        assert r.status_code == 200
        v = r.json()
        assert v["job_id"] == job and v["status"] == "PENDING" and v["result"] is None and v["created_at"]


async def test_consultar_una_tarea_inexistente_devuelve_404(settings):
    async with cliente_api(settings) as c:
        assert (await c.get("/tasks/" + "0" * 32)).status_code == 404
        assert (await c.get("/tasks/no-es-un-id")).status_code == 404
        assert (await c.get("/tasks/..%2F..%2Fetc")).status_code == 404


@pytest.mark.parametrize("cuerpo", [
    {}, {**CUERPO_OK, "request": ""}, {**CUERPO_OK, "request": "   "}, {**CUERPO_OK, "source_paths": []},
    {**CUERPO_OK, "target_paths": []}, {**CUERPO_OK, "max_steps": 1}, {**CUERPO_OK, "max_steps": 500},
    {**CUERPO_OK, "source_paths": [""]}, {**CUERPO_OK, "require_approval": "quizás"},
    {**CUERPO_OK, "source_paths": ["no/existe.json"]},
    {**CUERPO_OK, "source_paths": ["../../etc/passwd"]}, {**CUERPO_OK, "target_paths": ["/etc"]},
])
async def test_solicitudes_incorrectas_se_rechazan_con_422(settings, cuerpo):
    async with cliente_api(settings) as c:
        r = await c.post("/tasks", json=cuerpo)
        assert r.status_code == 422, r.text
        assert await c.app_ref.state.queue.largo_pendientes() == 0       # no se encola nada


async def test_json_malformado_devuelve_422(settings):
    async with cliente_api(settings) as c:
        r = await c.post("/tasks", content=b"{no es json", headers={"content-type": "application/json"})
        assert r.status_code == 422


async def test_openai_sin_credenciales_devuelve_un_error_de_configuracion_claro(redis_url, tmp_path):
    s = hacer_settings(redis_url, tmp_path, LLM_PROVIDER="openai")
    async with cliente_api(s) as c:
        r = await c.post("/tasks", json=CUERPO_OK)
        assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
        assert (await c.get("/health")).json()["llm_configured"] is False       # sin cambiar a modo local en silencio
        assert await c.app_ref.state.queue.largo_pendientes() == 0


async def test_una_excepcion_del_worker_produce_failed_visible_por_la_api(settings, clientes):
    def roto(_):
        raise RuntimeError("explotó el análisis")
    saver = RedisCheckpointSaver(async_client=clientes[1], prefix=settings.redis_prefix)
    grafo = construir_grafo(checkpointer=saver, settings=settings, analysis=roto)
    from dataclasses import replace
    s = replace(settings, embedded_worker=True)
    async with cliente_api(s, grafo=grafo) as c:
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        t = await esperar_http(c, job, {"FAILED"})
        assert "explotó el análisis" in t["error"] and t["finished_at"]


async def test_aprobar_tarea_inexistente_404_y_no_pendiente_409(settings):
    async with cliente_api(settings) as c:
        cuerpo = {"decision": "approve", "approver": "ana"}
        assert (await c.post("/tasks/" + "0" * 32 + "/approve", json=cuerpo)).status_code == 404
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        r = await c.post(f"/tasks/{job}/approve", json=cuerpo)           # PENDING, no WAITING_APPROVAL
        assert r.status_code == 409 and "PENDING" in r.json()["detail"]


@pytest.mark.parametrize("cuerpo", [{}, {"decision": "quizás", "approver": "a"}, {"decision": "approve"},
                                    {"decision": "approve", "approver": ""}, {"approver": "a"}])
async def test_aprobacion_con_cuerpo_invalido_422(settings, cuerpo):
    async with cliente_api(settings) as c:
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        assert (await c.post(f"/tasks/{job}/approve", json=cuerpo)).status_code == 422


async def test_token_de_aprobacion(redis_url, tmp_path):
    s = hacer_settings(redis_url, tmp_path, APPROVAL_TOKEN="secreto-123", EMBEDDED_WORKER="true")
    async with cliente_api(s) as c:
        job = (await c.post("/tasks", json=CUERPO_OK)).json()["job_id"]
        await esperar_http(c, job, {"WAITING_APPROVAL"})
        cuerpo = {"decision": "approve", "approver": "ana"}
        assert (await c.post(f"/tasks/{job}/approve", json=cuerpo)).status_code == 401
        assert (await c.post(f"/tasks/{job}/approve", json=cuerpo, headers={"X-Approval-Token": "mal"})).status_code == 401
        assert (await c.get(f"/tasks/{job}")).json()["status"] == "WAITING_APPROVAL"       # nada cambió
        ok = await c.post(f"/tasks/{job}/approve", json=cuerpo, headers={"X-Approval-Token": "secreto-123"})
        assert ok.status_code == 202 and ok.json()["decision"] == "approve"


async def test_en_produccion_sin_token_el_endpoint_de_aprobacion_esta_cerrado(redis_url, tmp_path):
    s = hacer_settings(redis_url, tmp_path, APP_ENV="production")
    async with cliente_api(s) as c:
        r = await c.post("/tasks/" + "0" * 32 + "/approve", json={"decision": "approve", "approver": "a"})
        assert r.status_code == 503 and "APPROVAL_TOKEN" in r.json()["detail"]


async def test_health_con_redis_disponible(settings):
    async with cliente_api(settings) as c:
        r = await c.get("/health")
        h = r.json()
        assert r.status_code == 200 and h["status"] == "ok" and h["redis"] is True and h["llm_provider"] == "local"
        assert h["langsmith_tracing"] is False
        assert "key" not in str(h).lower() and "redis://" not in str(h)       # no expone credenciales ni URLs


async def test_health_degradado_si_redis_no_responde(tmp_path):
    s = hacer_settings("redis://127.0.0.1:1/0", tmp_path)
    async with cliente_api(s) as c:
        r = await c.get("/health")
        assert r.status_code == 200 and r.json() == {**r.json(), "status": "degraded", "redis": False}
        r2 = await c.post("/tasks", json=CUERPO_OK)
        assert r2.status_code == 503 and "Redis" in r2.json()["detail"]


async def test_openapi_documenta_los_endpoints(settings):
    async with cliente_api(settings) as c:
        spec = (await c.get("/openapi.json")).json()
        assert {"/tasks", "/tasks/{job_id}", "/tasks/{job_id}/approve", "/health"} <= set(spec["paths"])
        assert (await c.get("/docs")).status_code == 200
