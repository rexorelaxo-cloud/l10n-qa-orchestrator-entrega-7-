"""Estado operativo de las tareas en Redis (separado de los checkpoints de LangGraph).

Cada tarea es un hash ``{prefijo}:task:{job_id}``. ``status`` se guarda como texto plano y el resto de los
campos como JSON. Las transiciones de estado son atómicas (script Lua con comparación y cambio), por lo
que dos aprobaciones o dos workers concurrentes no pueden resolver la misma tarea dos veces. Las
operaciones que además deben encolar trabajo lo hacen dentro del mismo script: si el proceso se cae, la
tarea no puede quedar «cambiada pero sin encolar».
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

from app.schemas import ESTADOS_FINALES, TRANSICIONES_VALIDAS, TaskStatus

_LUA_TRANSICION = """
local cur = redis.call('HGET', KEYS[1], 'status')
if not cur then return -1 end
local n = tonumber(ARGV[1])
local ok = 0
for i = 1, n do if ARGV[1 + i] == cur then ok = 1 end end
if ok == 0 then return 0 end
local args = {'status', ARGV[n + 2]}
local enqueue = ARGV[n + 3]
for i = n + 4, #ARGV do table.insert(args, ARGV[i]) end
redis.call('HSET', KEYS[1], unpack(args))
if enqueue ~= '' then redis.call('RPUSH', KEYS[2], enqueue) end
return 1
"""


class TransicionInvalida(Exception):
    pass


class TaskStore:
    def __init__(self, client, prefix: str = "l10n", ttl_seconds: int = 7 * 24 * 3600) -> None:
        self.r = client
        self.p = prefix
        self.ttl = ttl_seconds
        self._lua = client.register_script(_LUA_TRANSICION)

    # --- claves -------------------------------------------------------------------------------------
    def key(self, job_id: str) -> str: return f"{self.p}:task:{job_id}"
    @property
    def index_key(self) -> str: return f"{self.p}:tasks:index"
    @property
    def pending_key(self) -> str: return f"{self.p}:queue:pending"

    @staticmethod
    def _codificar(campos: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for k, v in campos.items():
            out += [k, json.dumps(v, ensure_ascii=False, default=str)]
        return out

    # --- operaciones --------------------------------------------------------------------------------
    async def crear_y_encolar(self, job_id: str, thread_id: str, payload: dict, mensaje_cola: str) -> dict:
        """Crea la tarea en PENDING y la encola en una única transacción (todo o nada)."""
        ahora = time.time()
        tarea = {"job_id": job_id, "thread_id": thread_id, "created_ts": ahora, "attempts": 0,
                 "payload": payload, "metrics": {}}
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(self.key(job_id), mapping={"status": TaskStatus.PENDING.value, **dict(zip(
            *[iter(self._codificar(tarea))] * 2))})
        pipe.zadd(self.index_key, {job_id: ahora})
        pipe.rpush(self.pending_key, mensaje_cola)
        await pipe.execute()
        return {**tarea, "status": TaskStatus.PENDING.value}

    async def obtener(self, job_id: str) -> Optional[dict]:
        h = await self.r.hgetall(self.key(job_id))
        if not h:
            return None
        return {k: (v if k == "status" else json.loads(v)) for k, v in h.items()}

    async def transicionar(self, job_id: str, desde: Iterable[TaskStatus], hacia: TaskStatus,
                           encolar: str = "", **campos: Any) -> bool:
        """Cambio atómico de estado. Devuelve ``False`` si el estado actual no estaba en ``desde``."""
        desde = list(desde)
        for d in desde:
            if hacia not in TRANSICIONES_VALIDAS[d]:
                raise TransicionInvalida(f"Transición no permitida: {d.value} → {hacia.value}")
        argv = [str(len(desde)), *[d.value for d in desde], hacia.value, encolar, *self._codificar(campos)]
        resultado = await self._lua(keys=[self.key(job_id), self.pending_key], args=argv)
        if resultado == 1 and hacia in ESTADOS_FINALES:
            await self.r.expire(self.key(job_id), self.ttl)
        return resultado == 1

    async def actualizar(self, job_id: str, **campos: Any) -> None:
        if campos:
            await self.r.hset(self.key(job_id), mapping=dict(zip(*[iter(self._codificar(campos))] * 2)))

    async def ids_recientes(self, limite: int = 1000) -> list[str]:
        return list(await self.r.zrevrange(self.index_key, 0, limite - 1))

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:
            return False
