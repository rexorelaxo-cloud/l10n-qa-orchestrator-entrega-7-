"""Worker asíncrono: consume la cola de Redis y ejecuta el grafo de LangGraph en segundo plano.

Garantías (dentro de lo que ofrece la implementación):
* Concurrencia limitada por ``WORKER_CONCURRENCY`` (semáforo) y aislamiento por ``thread_id``.
* Toda tarea reclamada termina en un estado final o queda en ``WAITING_APPROVAL``; si el worker muere, la
  reserva (``lease_until``) vence y ``recuperar_huerfanas`` la reencola o la marca ``FAILED``.
* Los reintentos son acotados (``MAX_ATTEMPTS``) y solo para fallas transitorias (red, límites de tasa).
  Reintentar nunca duplica la operación crítica: el grafo continúa desde el checkpoint y la importación
  simulada se crea de forma exclusiva.
* Los errores se guardan con un mensaje útil y sin credenciales.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import socket
import time
from typing import Any, Callable, Optional

from langgraph.types import Command
from redis.exceptions import RedisError

from app.checkpointer import RedisCheckpointSaver
from app.config import ConfigError, Settings
from app.graph import construir_grafo
from app.llm.factory import crear_llm
from app.observability import config_ejecucion, configurar_langsmith
from app.queue import TaskQueue
from app.redis_store import TaskStore
from app.schemas import ESTADOS_FINALES, TaskStatus
from app.state import crear_estado_inicial

log = logging.getLogger("l10n.worker")
_SECRETO = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|lsv2_[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")


def sanitizar_error(exc: BaseException, limite: int = 400) -> str:
    """Mensaje de error seguro: sin claves ni rutas internas excesivas."""
    if isinstance(exc, ConfigError):
        return f"Error de configuración: {exc}"
    texto = _SECRETO.sub("[oculto]", f"{type(exc).__name__}: {exc}")
    return texto[:limite]


def es_transitorio(exc: BaseException) -> bool:
    """Fallas que justifican un reintento: red/Redis/timeouts y límites de tasa del proveedor."""
    if isinstance(exc, (ConnectionError, TimeoutError, RedisError)):
        return True
    nombre = type(exc).__name__
    return nombre in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}


def construir_resultado(valores: dict) -> dict:
    """Resultado público de la tarea, derivado del estado final del grafo (no de la memoria del worker)."""
    informe = valores.get("final_report") or {}
    hallazgos = []
    for sev in ("error", "warning", "info"):
        for h in informe.get("hallazgos_por_severidad", {}).get(sev, []):
            hallazgos.append({"id": h.get("id"), "severidad": sev, "categoria": h.get("categoria"),
                              "regla": h.get("regla"), "archivo": h.get("archivo"),
                              "ruta_json": h.get("ruta_json"), "mensaje": h.get("mensaje"),
                              "evidencia": h.get("evidencia"), "estado": h.get("estado")})
    agentes = []
    for c in valores.get("agent_contributions", []):
        agentes.append({"paso": c.get("paso"), "agente": c.get("agente"), "etapa": c.get("tipo_analisis"),
                        "resumen": c.get("resumen")})
    return {
        "estado_general": informe.get("estado_general"),
        "estado_general_es": informe.get("estado_general_es"),
        "resumen": informe.get("resumen"),
        "archivos_analizados": informe.get("archivos_analizados"),
        "archivos_no_analizados": informe.get("archivos_no_analizados"),
        "hallazgos": hallazgos,
        "agentes_y_etapas": agentes,
        "decisiones_supervisor": informe.get("decisiones_supervisor"),
        "pasos_ejecutados": informe.get("pasos_ejecutados"),
        "problemas_pendientes": informe.get("problemas_pendientes"),
        "recomendaciones": informe.get("recomendaciones"),
        "resumen_llm": valores.get("resumen_llm"),
        "importacion": {"estado": valores.get("import_status"), "resultado": valores.get("import_result"),
                        "aprobacion": valores.get("approval")},
    }


class FabricaGrafo:
    """Construye una sola vez (de forma perezosa) el grafo con el checkpointer de Redis y el proveedor LLM."""

    def __init__(self, settings: Settings, redis_bytes, llm=None, grafo=None) -> None:
        """``llm``: ManejadorLLM ya creado (pruebas). ``grafo``: grafo ya compilado (pruebas)."""
        self.settings, self._redis_bytes, self._llm = settings, redis_bytes, llm
        self._grafo = grafo
        self._lock = asyncio.Lock()
        self.handle = llm if grafo is not None and llm is not None else None
        if grafo is not None and self.handle is None:
            from app.llm.factory import ManejadorLLM
            self.handle = ManejadorLLM("local", "ninguno (determinista)", False)

    async def obtener(self):
        async with self._lock:
            if self._grafo is None:
                self.handle = self._llm or crear_llm(self.settings)     # puede lanzar ConfigError
                saver = RedisCheckpointSaver(async_client=self._redis_bytes, prefix=self.settings.redis_prefix,
                                             ttl_seconds=self.settings.task_ttl_seconds)
                self._grafo = construir_grafo(checkpointer=saver, llm=self.handle, settings=self.settings)
            return self._grafo


class Worker:
    def __init__(self, settings: Settings, store: TaskStore, queue: TaskQueue, fabrica: FabricaGrafo,
                 worker_id: Optional[str] = None) -> None:
        self.s, self.store, self.queue, self.fabrica = settings, store, queue, fabrica
        self.id = worker_id or f"{socket.gethostname()}-{id(self) & 0xFFFF:x}"
        self._sem = asyncio.Semaphore(settings.worker_concurrency)
        self._tareas: set[asyncio.Task] = set()
        self._activos: set[str] = set()
        self._detener = asyncio.Event()

    # ------------------------------------------------------------------ ciclo principal
    async def run_forever(self) -> None:
        log.info("Worker %s iniciado (concurrencia=%s)", self.id, self.s.worker_concurrency)
        ultimo_barrido = 0.0
        while not self._detener.is_set():
            try:
                if time.time() - ultimo_barrido > self.s.lease_seconds / 2:
                    await self.recuperar_huerfanas()
                    ultimo_barrido = time.time()
                await self._sem.acquire()
                raw = None
                try:
                    raw = await self.queue.reclamar()
                except BaseException:
                    self._sem.release()
                    raise
                if raw is None:
                    self._sem.release()
                    await asyncio.sleep(self.s.poll_interval)
                    continue
                t = asyncio.create_task(self._procesar_y_liberar(raw))
                self._tareas.add(t)
                t.add_done_callback(self._tareas.discard)
            except asyncio.CancelledError:
                raise
            except RedisError as exc:
                log.warning("Redis no disponible (%s); reintentando en 1 s.", type(exc).__name__)
                await asyncio.sleep(1)
            except Exception:                                             # el worker no debe morir
                log.exception("Error inesperado en el ciclo del worker")
                await asyncio.sleep(1)

    async def detener(self, gracia: float = 30.0) -> None:
        self._detener.set()
        if self._tareas:
            _, pendientes = await asyncio.wait(self._tareas, timeout=gracia)
            for t in pendientes:
                t.cancel()       # la tarea queda RUNNING; su reserva vence y se recupera

    async def procesar_hasta_vaciar(self, max_espera: float = 60.0) -> None:
        """Utilidad para pruebas: procesa hasta que no quede nada pendiente ni en curso."""
        fin = time.time() + max_espera
        while time.time() < fin:
            raw = None
            if self._sem._value > 0:
                await self._sem.acquire()
                raw = await self.queue.reclamar()
                if raw is None:
                    self._sem.release()
                else:
                    t = asyncio.create_task(self._procesar_y_liberar(raw))
                    self._tareas.add(t)
                    t.add_done_callback(self._tareas.discard)
            if raw is None:
                if not self._tareas and await self.queue.largo_pendientes() == 0:
                    return
                await asyncio.sleep(0.02)

    # ------------------------------------------------------------------ recuperación
    async def recuperar_huerfanas(self) -> int:
        recuperadas = 0
        ahora = time.time()
        for raw, reclamado in await self.queue.en_proceso():
            if raw in self._activos:
                continue
            msg = json.loads(raw)
            tarea = await self.store.obtener(msg["job_id"])
            estado = TaskStatus(tarea["status"]) if tarea else None
            if tarea is None or estado in ESTADOS_FINALES or estado == TaskStatus.WAITING_APPROVAL:
                await self.queue.confirmar(raw)
                continue
            vencida = (tarea.get("lease_until") or 0) < ahora if estado == TaskStatus.RUNNING \
                else reclamado < ahora - self.s.lease_seconds
            if not vencida:
                continue
            await self.queue.confirmar(raw)
            job_id = msg["job_id"]
            if tarea.get("attempts", 0) >= self.s.max_attempts:
                await self.store.transicionar(
                    job_id, [estado], TaskStatus.FAILED, finished_ts=ahora,
                    error="La tarea se interrumpió y superó el máximo de intentos permitidos.")
                log.warning("Tarea %s marcada FAILED tras agotar intentos", job_id)
            else:
                nuevo = json.dumps({"job_id": job_id, "kind": msg["kind"], "n": f"rec-{int(ahora * 1000)}"},
                                   sort_keys=True)
                ok = await self.store.transicionar(job_id, [estado], TaskStatus.PENDING, encolar=nuevo,
                                                   lease_until=None, recuperada_ts=ahora)
                if not ok and estado == TaskStatus.PENDING:        # ya estaba PENDING: solo reencolar
                    await self.queue.r.rpush(self.queue.pending, nuevo)
                log.warning("Tarea %s recuperada tras una interrupción (estado previo %s)", job_id, estado.value)
            recuperadas += 1
        return recuperadas

    # ------------------------------------------------------------------ procesamiento de una tarea
    async def _procesar_y_liberar(self, raw: str) -> None:
        self._activos.add(raw)
        try:
            await self._procesar(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Fallo no controlado procesando %s", raw)
        finally:
            self._activos.discard(raw)
            self._sem.release()

    async def _latido(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.s.lease_seconds / 3))
            try:
                await self.store.transicionar(job_id, [TaskStatus.RUNNING], TaskStatus.RUNNING,
                                              lease_until=time.time() + self.s.lease_seconds)
            except RedisError:
                pass

    async def _procesar(self, raw: str) -> None:
        msg = json.loads(raw)
        job_id, kind = msg["job_id"], msg["kind"]
        tarea = await self.store.obtener(job_id)
        if tarea is None:
            await self.queue.confirmar(raw)
            return
        t0 = time.time()
        intento = int(tarea.get("attempts", 0)) + 1
        reclamada = await self.store.transicionar(
            job_id, [TaskStatus.PENDING], TaskStatus.RUNNING, attempts=intento,
            lease_until=t0 + self.s.lease_seconds, worker_id=self.id, segmento_ts=t0,
            started_ts=tarea.get("started_ts") or t0)
        if not reclamada:                      # entrega duplicada o tarea ya resuelta: no se ejecuta nada
            await self.queue.confirmar(raw)
            return
        latido = asyncio.create_task(self._latido(job_id))
        try:
            await self._ejecutar(job_id, kind, tarea, intento, t0)
        except asyncio.CancelledError:
            raise                               # apagado: la reserva vence y la tarea se recupera
        except Exception as exc:
            await self._manejar_error(job_id, kind, exc, intento, t0, tarea)
        finally:
            latido.cancel()
            try:
                await self.queue.confirmar(raw)
            except RedisError:
                log.warning("No se pudo confirmar %s; se resolverá en la recuperación", raw)

    async def _ejecutar(self, job_id: str, kind: str, tarea: dict, intento: int, t0: float) -> None:
        grafo = await self.fabrica.obtener()
        payload, thread_id = tarea["payload"], tarea["thread_id"]
        h = self.fabrica.handle
        cfg = config_ejecucion(job_id, thread_id, proveedor=h.proveedor, modelo=h.modelo, intento=intento,
                               extra={"kind": kind})
        snap = await grafo.aget_state(cfg)
        entrada: Any = None
        if kind == "resume":
            if snap.next:
                entrada = Command(resume=tarea["approval"])
        elif not snap.values:                                   # primera ejecución
            entrada = crear_estado_inicial(
                payload["request"], payload["source_paths"], payload["target_paths"],
                payload.get("max_steps") or self.s.max_steps, job_id=job_id, thread_id=thread_id,
                require_approval=payload.get("require_approval", True))
        if entrada is not None or snap.next:
            await grafo.ainvoke(entrada, cfg)
        snap = await grafo.aget_state(cfg)
        interrupciones = [i for t in snap.tasks for i in t.interrupts]
        ahora = time.time()
        previo = tarea.get("metrics") or {}
        acumulado = float(previo.get("execution_s", 0.0)) + (ahora - t0)
        etapas = [e for e in (snap.values or {}).get("stage_timings", [])]
        metricas = {
            "queue_wait_s": round((tarea.get("started_ts") or t0) - tarea["created_ts"], 4),
            "execution_s": round(acumulado, 4),
            "stages": etapas,
            "llm": (snap.values or {}).get("resumen_llm"),
            "attempts": intento,
        }
        if interrupciones:
            ok = await self.store.transicionar(
                job_id, [TaskStatus.RUNNING], TaskStatus.WAITING_APPROVAL, lease_until=None,
                approval_request=interrupciones[0].value, metrics=metricas)
            log.info("Tarea %s en espera de aprobación (%s)", job_id, ok)
            return
        valores = snap.values
        rechazada = valores.get("import_status") == "RECHAZADA"
        estado = TaskStatus.REJECTED if rechazada else TaskStatus.DONE
        metricas["total_s"] = round(ahora - tarea["created_ts"], 4)
        await self.store.transicionar(
            job_id, [TaskStatus.RUNNING], estado, lease_until=None, finished_ts=ahora,
            result=construir_resultado(valores), metrics=metricas, error=None)

    async def _manejar_error(self, job_id: str, kind: str, exc: BaseException, intento: int, t0: float,
                             tarea: dict) -> None:
        mensaje = sanitizar_error(exc)
        if es_transitorio(exc) and intento < self.s.max_attempts:
            log.warning("Falla transitoria en %s (intento %s/%s): %s", job_id, intento, self.s.max_attempts, mensaje)
            await asyncio.sleep(min(2 ** (intento - 1) * 0.5, 5))
            nuevo = json.dumps({"job_id": job_id, "kind": kind, "n": f"retry-{intento}-{int(time.time() * 1000)}"},
                               sort_keys=True)
            await self.store.transicionar(job_id, [TaskStatus.RUNNING], TaskStatus.PENDING, encolar=nuevo,
                                          lease_until=None, ultimo_error=mensaje)
            return
        log.error("Tarea %s falló: %s", job_id, mensaje)
        ahora = time.time()
        previo = (await self.store.obtener(job_id) or {}).get("metrics") or {}
        await self.store.transicionar(
            job_id, [TaskStatus.RUNNING], TaskStatus.FAILED, lease_until=None, finished_ts=ahora, error=mensaje,
            metrics={**previo, "execution_s": round(float(previo.get("execution_s", 0)) + ahora - t0, 4),
                     "total_s": round(ahora - tarea["created_ts"], 4), "attempts": intento})


# ---------------------------------------------------------------------------------------------- proceso
def crear_clientes_redis(settings: Settings):
    import redis.asyncio as aioredis
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RConn, TimeoutError as RTime
    kw = dict(socket_connect_timeout=5, health_check_interval=30,
              retry=Retry(ExponentialBackoff(cap=2, base=0.1), 5), retry_on_error=[RConn, RTime])
    return (aioredis.from_url(settings.redis_url, decode_responses=True, **kw),
            aioredis.from_url(settings.redis_url, decode_responses=False, **kw))


def crear_worker(settings: Settings, llm=None, grafo=None) -> tuple[Worker, Callable]:
    texto, binario = crear_clientes_redis(settings)
    store = TaskStore(texto, settings.redis_prefix, settings.task_ttl_seconds)
    queue = TaskQueue(texto, settings.redis_prefix)
    worker = Worker(settings, store, queue, FabricaGrafo(settings, binario, llm, grafo))

    async def cerrar() -> None:
        await texto.aclose()
        await binario.aclose()
    return worker, cerrar


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    log.info("Trazas LangSmith: %s", configurar_langsmith(settings))
    worker, cerrar = crear_worker(settings)
    tarea = asyncio.create_task(worker.run_forever())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, tarea.cancel)
        except NotImplementedError:   # Windows
            pass
    try:
        await tarea
    except asyncio.CancelledError:
        pass
    finally:
        await worker.detener()
        await cerrar()


if __name__ == "__main__":
    asyncio.run(_main())
