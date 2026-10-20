"""API REST (FastAPI) del L10n QA Orchestrator.

La API NO ejecuta el grafo: valida, persiste el estado inicial en Redis, encola el trabajo y responde
``202``. El análisis lo realiza el worker (proceso aparte, o embebido con ``EMBEDDED_WORKER=true``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.config import ConfigError, Settings
from app.llm.factory import validar_configuracion
from app.observability import configurar_langsmith
from app.queue import TaskQueue, crear_mensaje
from app.redis_store import TaskStore
from app.schemas import (ApprovalRequest, ApprovalResponse, HealthResponse, TaskCreatedResponse,
                         TaskCreateRequest, TaskStatus, TaskView, iso)
from app.worker import FabricaGrafo, Worker, crear_clientes_redis

log = logging.getLogger("l10n.api")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def crear_app(settings: Optional[Settings] = None, *, llm=None, grafo=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        texto, binario = crear_clientes_redis(settings)
        app.state.settings = settings
        app.state.redis = texto
        app.state.store = TaskStore(texto, settings.redis_prefix, settings.task_ttl_seconds)
        app.state.queue = TaskQueue(texto, settings.redis_prefix)
        app.state.langsmith = configurar_langsmith(settings)
        worker_task = None
        if settings.embedded_worker:
            app.state.worker = Worker(settings, app.state.store, app.state.queue,
                                      FabricaGrafo(settings, binario, llm, grafo))
            worker_task = asyncio.create_task(app.state.worker.run_forever())
        try:
            yield
        finally:
            if worker_task:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
                await app.state.worker.detener(gracia=5)
            await texto.aclose()
            await binario.aclose()

    app = FastAPI(
        title="L10n QA Orchestrator", version="1.0.0", lifespan=lifespan,
        description=("QA estructural de archivos JSON localizados antes de importarlos a un CMS. "
                     "Las tareas se encolan en Redis y las procesa un worker con LangGraph; la preparación del "
                     "paquete de importación (simulada) requiere aprobación humana."))

    @app.exception_handler(RedisError)
    async def _redis_error(_: Request, exc: RedisError):
        log.error("Redis no disponible: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Redis no está disponible en este momento."})

    def _resolver_rutas(rutas: list[str], campo: str) -> list[str]:
        raiz = settings.input_root
        resultado = []
        for r in rutas:
            p = Path(r)
            p = (p if p.is_absolute() else raiz / p).resolve()
            if not p.is_relative_to(raiz):
                raise HTTPException(422, f"{campo}: la ruta {r!r} queda fuera de la carpeta permitida (INPUT_ROOT).")
            if not p.exists():
                raise HTTPException(422, f"{campo}: la ruta {r!r} no existe.")
            resultado.append(str(p))
        return resultado

    @app.post("/tasks", status_code=202, response_model=TaskCreatedResponse, tags=["tareas"],
              summary="Crea una tarea de análisis (asíncrona)")
    async def crear_tarea(body: TaskCreateRequest, request: Request, response: Response):
        t0 = time.perf_counter()
        origen = _resolver_rutas(body.source_paths, "source_paths")
        destino = _resolver_rutas(body.target_paths, "target_paths")
        try:
            validar_configuracion(settings)
        except ConfigError as exc:
            raise HTTPException(503, f"Error de configuración: {exc}") from exc
        job_id = uuid.uuid4().hex
        thread_id = f"thread-{job_id}"
        payload = {"request": body.request, "source_paths": origen, "target_paths": destino,
                   "max_steps": body.max_steps, "require_approval": body.require_approval}
        await request.app.state.store.crear_y_encolar(job_id, thread_id, payload, crear_mensaje(job_id, "run"))
        url = f"/tasks/{job_id}"
        response.headers["Location"] = url
        return TaskCreatedResponse(job_id=job_id, thread_id=thread_id, status=TaskStatus.PENDING, status_url=url,
                                   enqueue_ms=round((time.perf_counter() - t0) * 1000, 3))

    def _vista(t: dict) -> TaskView:
        return TaskView(
            job_id=t["job_id"], thread_id=t["thread_id"], status=TaskStatus(t["status"]),
            created_at=iso(t.get("created_ts")), started_at=iso(t.get("started_ts")),
            finished_at=iso(t.get("finished_ts")), attempts=t.get("attempts", 0), error=t.get("error"),
            approval_request=t.get("approval_request") if t["status"] == "WAITING_APPROVAL" else None,
            approval=t.get("approval"), result=t.get("result"), metrics=t.get("metrics") or None)

    async def _obtener_o_404(request: Request, job_id: str) -> dict:
        if not _JOB_ID.match(job_id):
            raise HTTPException(404, "Tarea no encontrada.")
        tarea = await request.app.state.store.obtener(job_id)
        if tarea is None:
            raise HTTPException(404, "Tarea no encontrada.")
        return tarea

    @app.get("/tasks/{job_id}", response_model=TaskView, tags=["tareas"],
             summary="Estado y resultado de una tarea (leído desde Redis)")
    async def consultar_tarea(job_id: str, request: Request):
        return _vista(await _obtener_o_404(request, job_id))

    @app.post("/tasks/{job_id}/approve", status_code=202, response_model=ApprovalResponse, tags=["tareas"],
              summary="Aprueba o rechaza una operación que espera intervención humana")
    async def aprobar(job_id: str, body: ApprovalRequest, request: Request,
                      x_approval_token: Optional[str] = Header(None, description="Valor de APPROVAL_TOKEN")):
        if not settings.approval_token:
            if settings.app_env not in {"local", "test"}:
                raise HTTPException(503, "APPROVAL_TOKEN no está configurado: el endpoint de aprobación está cerrado.")
        elif not x_approval_token or not secrets.compare_digest(x_approval_token, settings.approval_token):
            raise HTTPException(401, "Token de aprobación inválido o ausente.")
        tarea = await _obtener_o_404(request, job_id)
        if tarea["status"] != TaskStatus.WAITING_APPROVAL.value:
            raise HTTPException(409, f"La tarea no está pendiente de aprobación (estado actual: {tarea['status']}).")
        ahora = time.time()
        registro = {"decision": body.decision, "approver": body.approver.strip(), "comment": body.comment,
                    "decided_at": iso(ahora)}
        ok = await request.app.state.store.transicionar(
            job_id, [TaskStatus.WAITING_APPROVAL], TaskStatus.PENDING,
            encolar=crear_mensaje(job_id, "resume"), approval=registro, approval_request=None)
        if not ok:                       # otra aprobación ganó la carrera
            actual = (await request.app.state.store.obtener(job_id) or {}).get("status")
            raise HTTPException(409, f"La tarea ya fue resuelta (estado actual: {actual}).")
        msg = ("Aprobación registrada: la reanudación quedó encolada." if body.decision == "approve"
               else "Rechazo registrado: el grafo se reanudará para cerrar sin ejecutar la operación crítica.")
        return ApprovalResponse(job_id=job_id, status=TaskStatus.PENDING, decision=body.decision,
                                approver=registro["approver"], decided_at=registro["decided_at"], message=msg)

    @app.get("/health", response_model=HealthResponse, tags=["sistema"], summary="Estado de la API y Redis")
    async def salud(request: Request):
        redis_ok, pendientes = False, None
        try:
            redis_ok = bool(await asyncio.wait_for(request.app.state.redis.ping(), timeout=2))
            pendientes = await request.app.state.queue.largo_pendientes()
        except Exception:
            redis_ok = False
        try:
            validar_configuracion(settings)
            llm_ok = True
        except ConfigError:
            llm_ok = False
        return HealthResponse(status="ok" if redis_ok else "degraded", redis=redis_ok,
                              llm_provider=settings.llm_provider, llm_configured=llm_ok,
                              langsmith_tracing=bool(request.app.state.langsmith.get("activo")),
                              queue_pending=pendientes)

    return app


def _app_por_defecto() -> FastAPI:
    return crear_app()


app = _app_por_defecto()
