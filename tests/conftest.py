"""Utilidades compartidas por las pruebas."""
import json
from pathlib import Path

import pytest

from app.agents.analyst_agent import analysis_node
from app.agents.research_agent import research_node
from app.agents.supervisor_agent import decidir_siguiente, supervisor_node
from app.cli import SOLICITUD_EJEMPLO
from app.state import crear_estado_inicial

RAIZ = Path(__file__).resolve().parent.parent
FIXTURES = RAIZ / "fixtures"
ORIGEN = str(FIXTURES / "source")
DESTINO = str(FIXTURES / "targets")

CAMPOS_ACUMULATIVOS = ("research_findings", "analysis_findings", "agent_contributions", "errors",
                       "refinement_log", "routing_log", "uncertainties", "conflicts", "messages")


def aplicar(estado: dict, update: dict) -> dict:
    """Emula los reducers del estado: acumula listas, suma pasos y reemplaza el resto."""
    nuevo = dict(estado)
    for k, v in update.items():
        if k in CAMPOS_ACUMULATIVOS:
            nuevo[k] = list(estado.get(k, [])) + list(v)
        elif k == "step_count":
            nuevo[k] = estado.get(k, 0) + v
        else:
            nuevo[k] = v
    return nuevo


def avanzar_hasta(estado: dict, ruta_objetivo: str) -> dict:
    """Ejecuta supervisor y especialistas reales hasta que el supervisor elija ``ruta_objetivo``."""
    nodos = {"research": research_node, "analysis": analysis_node}
    for _ in range(10):
        if decidir_siguiente(estado)["ruta"] == ruta_objetivo:
            return estado
        estado = aplicar(estado, supervisor_node(estado))
        estado = aplicar(estado, nodos[estado["next_agent"]](estado))
    raise AssertionError(f"El supervisor nunca eligió {ruta_objetivo}")


@pytest.fixture
def estado_base():
    return crear_estado_inicial(SOLICITUD_EJEMPLO, [ORIGEN], [DESTINO])


@pytest.fixture
def estado_investigado(estado_base):
    return avanzar_hasta(estado_base, "analysis")


@pytest.fixture
def estado_analizado(estado_investigado):
    est = aplicar(estado_investigado, supervisor_node(estado_investigado))
    return aplicar(est, analysis_node(est))


@pytest.fixture
def escribir_json(tmp_path):
    def _escribir(nombre: str, contenido, crudo: bool = False) -> str:
        ruta = tmp_path / nombre
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido if crudo else json.dumps(contenido, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(ruta)
    return _escribir


def firmas(hallazgos, archivo=None):
    """Conjunto de (severidad, categoría, ruta_json) para comparar con los resultados esperados."""
    return {(h["severidad"], h["categoria"], h["ruta_json"]) for h in hallazgos
            if archivo is None or Path(h["archivo"]).name == archivo}


def esperado(nombre: str) -> dict:
    return json.loads((FIXTURES / "expected_results" / nombre).read_text(encoding="utf-8"))


def firmas_esperadas(nombre: str):
    return {(h["severidad"], h["categoria"], h["ruta_json"]) for h in esperado(nombre)["hallazgos"]}


# =====================================================================================================
# Fixtures del Módulo 7: Redis REAL efímero, configuración aislada y utilidades asíncronas
# =====================================================================================================
import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest_asyncio

from app.config import Settings


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def redis_url():
    """URL de un Redis real. Usa REDIS_TEST_URL si existe; si no, levanta un ``redis-server`` efímero.
    Si no hay ninguno de los dos, las pruebas de Redis se omiten (con el motivo a la vista)."""
    externo = os.getenv("REDIS_TEST_URL")
    if externo:
        yield externo
        return
    binario = shutil.which("redis-server")
    if not binario:
        pytest.skip("No hay Redis disponible: instale redis-server o defina REDIS_TEST_URL.")
    puerto = _puerto_libre()
    proc = subprocess.Popen([binario, "--port", str(puerto), "--save", "", "--appendonly", "no",
                             "--bind", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis as _redis
    cliente = _redis.Redis(port=puerto)
    for _ in range(100):
        try:
            if cliente.ping():
                break
        except Exception:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.skip("redis-server no arrancó.")
    yield f"redis://127.0.0.1:{puerto}/0"
    proc.terminate()
    proc.wait(timeout=10)


def hacer_settings(redis_url: str, tmp_path, **extra) -> Settings:
    env = {"APP_ENV": "test", "LLM_PROVIDER": "local", "REDIS_URL": redis_url,
           "REDIS_PREFIX": f"t{uuid.uuid4().hex[:8]}", "OUTPUT_DIR": str(tmp_path / "salida"),
           "QUEUE_POLL_INTERVAL": "0.02", "LEASE_SECONDS": "5", "MAX_ATTEMPTS": "3",
           "WORKER_CONCURRENCY": "3", "INPUT_ROOT": str(FIXTURES), "TASK_TTL_SECONDS": "600"}
    env.update({k: str(v) for k, v in extra.items()})
    return Settings.from_env(env)


@pytest.fixture
def settings(redis_url, tmp_path):
    return hacer_settings(redis_url, tmp_path)


@pytest_asyncio.fixture
async def clientes(settings):
    from app.worker import crear_clientes_redis
    texto, binario = crear_clientes_redis(settings)
    yield texto, binario
    claves = await texto.keys(f"{settings.redis_prefix}:*")
    if claves:
        await texto.delete(*claves)
    await texto.aclose()
    await binario.aclose()


@pytest_asyncio.fixture
async def entorno(settings, clientes):
    """Store, cola y worker reales sobre Redis real (sin servidor HTTP)."""
    from app.queue import TaskQueue
    from app.redis_store import TaskStore
    from app.worker import FabricaGrafo, Worker
    texto, binario = clientes
    store = TaskStore(texto, settings.redis_prefix, settings.task_ttl_seconds)
    queue = TaskQueue(texto, settings.redis_prefix)
    worker = Worker(settings, store, queue, FabricaGrafo(settings, binario), worker_id="w-test")

    class E:  # noqa: N801
        pass
    e = E()
    e.settings, e.store, e.queue, e.worker, e.texto, e.binario = settings, store, queue, worker, texto, binario
    return e


async def crear_tarea_directa(e, origen="source/clean.en.json", destino="targets/clean.es.json",
                              aprobacion=True, max_steps=None, solicitud="QA estructural de localización"):
    """Crea una tarea como lo hace la API (persistida + encolada) sin pasar por HTTP."""
    from app.queue import crear_mensaje
    job_id = uuid.uuid4().hex
    payload = {"request": solicitud, "source_paths": [str(FIXTURES / origen)],
               "target_paths": [str(FIXTURES / destino)], "max_steps": max_steps, "require_approval": aprobacion}
    await e.store.crear_y_encolar(job_id, f"thread-{job_id}", payload, crear_mensaje(job_id, "run"))
    return job_id


async def esperar_estado(obtener, estados, timeout=20.0):
    """Sondea ``obtener()`` (corrutina que devuelve la tarea) hasta que su estado esté en ``estados``."""
    fin = time.time() + timeout
    ultimo = None
    while time.time() < fin:
        ultimo = await obtener()
        if ultimo and ultimo["status"] in estados:
            return ultimo
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timeout esperando {estados}; último estado: {ultimo and ultimo['status']}")


from contextlib import asynccontextmanager

import httpx


@asynccontextmanager
async def cliente_api(settings, **kw):
    """API real (FastAPI + Redis real) servida en memoria con httpx; opcionalmente con worker embebido."""
    from app.main import crear_app
    app = crear_app(settings, **kw)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
            c.app_ref = app
            yield c


CUERPO_OK = {"request": "QA estructural de localización", "source_paths": ["source/clean.en.json"],
             "target_paths": ["targets/clean.es.json"]}


async def esperar_http(c, job_id, estados, timeout=20.0):
    fin = time.time() + timeout
    ultimo = None
    while time.time() < fin:
        ultimo = (await c.get(f"/tasks/{job_id}")).json()
        if ultimo["status"] in estados:
            return ultimo
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timeout esperando {estados}; último: {ultimo and ultimo['status']} {ultimo and ultimo.get('error')}")
