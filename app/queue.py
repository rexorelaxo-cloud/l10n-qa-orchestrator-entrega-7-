"""Cola de trabajo persistente sobre Redis (patrón «cola confiable»).

* ``{p}:queue:pending``     lista con los mensajes a procesar (RPUSH al encolar).
* ``{p}:queue:processing``  lista con los mensajes reclamados por algún worker.
* ``{p}:queue:claims``      hash mensaje → instante en que se reclamó.

Reclamar un mensaje lo mueve de forma atómica de ``pending`` a ``processing`` (``LMOVE`` dentro de un script
Lua). El worker lo confirma (``confirmar``) solo cuando termina; si el proceso se cae antes, el mensaje
sigue en ``processing`` y ``Worker.recuperar_huerfanas`` lo reencola o falla la tarea. La garantía es
«al menos una vez»; la idempotencia la aporta el estado de la tarea (transición atómica a RUNNING) y el
checkpoint de LangGraph, no la cola.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

_LUA_RECLAMAR = """
local raw = redis.call('LMOVE', KEYS[1], KEYS[2], 'LEFT', 'RIGHT')
if not raw then return false end
redis.call('HSET', KEYS[3], raw, ARGV[1])
return raw
"""


def crear_mensaje(job_id: str, kind: str) -> str:
    """``kind``: ``run`` (primera ejecución o continuación) o ``resume`` (tras la decisión humana).
    El identificador ``n`` hace único cada mensaje, de modo que ``LREM`` nunca borra otro distinto."""
    return json.dumps({"job_id": job_id, "kind": kind, "n": uuid.uuid4().hex}, sort_keys=True)


class TaskQueue:
    def __init__(self, client, prefix: str = "l10n") -> None:
        self.r = client
        self.pending = f"{prefix}:queue:pending"
        self.processing = f"{prefix}:queue:processing"
        self.claims = f"{prefix}:queue:claims"
        self._reclamar = client.register_script(_LUA_RECLAMAR)

    async def encolar(self, job_id: str, kind: str = "run") -> str:
        raw = crear_mensaje(job_id, kind)
        await self.r.rpush(self.pending, raw)
        return raw

    async def reclamar(self) -> Optional[str]:
        raw = await self._reclamar(keys=[self.pending, self.processing, self.claims], args=[str(time.time())])
        return raw or None

    async def confirmar(self, raw: str) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.lrem(self.processing, 1, raw)
        pipe.hdel(self.claims, raw)
        await pipe.execute()

    async def en_proceso(self) -> list[tuple[str, float]]:
        reclamos = await self.r.hgetall(self.claims)
        return [(raw, float(ts)) for raw, ts in reclamos.items()]

    async def largo_pendientes(self) -> int:
        return int(await self.r.llen(self.pending))
