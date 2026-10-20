"""Checkpointer de LangGraph sobre Redis (almacén de checkpoints, separado del estado operativo).

Por qué existe: el paquete oficial ``langgraph-checkpoint-redis`` (``RedisSaver``) exige Redis Stack
(módulos RediSearch y RedisJSON). Este proyecto debe funcionar con un Redis estándar, así que se
implementa ``BaseCheckpointSaver`` (la interfaz pública de LangGraph) usando únicamente comandos
básicos de Redis. Los checkpoints son reales y persistentes: no hay diccionarios en memoria.

Modelo de claves (prefijo configurable, ``l10n`` por defecto):
    {p}:ckpt:{thread}:{ns}:{id}      hash  → checkpoint y metadatos serializados + checkpoint padre
    {p}:ckpt_idx:{thread}:{ns}       zset  → ids de checkpoint (puntaje 0, orden lexicográfico = cronológico)
    {p}:ckpt_ns:{thread}             set   → espacios de nombres usados por el hilo
    {p}:writes:{thread}:{ns}:{id}    hash  → escrituras pendientes de las tareas (incluye ``interrupt``)

El ``thread_id`` aísla por completo las ejecuciones: ninguna operación cruza hilos.
"""
from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (WRITES_IDX_MAP, BaseCheckpointSaver, ChannelVersions, Checkpoint,
                                       CheckpointMetadata, CheckpointTuple, get_checkpoint_id,
                                       get_checkpoint_metadata, writes_sort_key)

SEP = "\x1f"


def _b(x: Any) -> bytes:
    return x if isinstance(x, bytes) else str(x).encode()


class RedisCheckpointSaver(BaseCheckpointSaver[str]):
    """Checkpointer persistente. Recibe clientes ``redis.Redis`` y/o ``redis.asyncio.Redis`` SIN
    ``decode_responses`` (se guardan bytes). Con ``ttl_seconds`` los datos del hilo expiran."""

    def __init__(self, *, sync_client=None, async_client=None, prefix: str = "l10n",
                 ttl_seconds: Optional[int] = None, serde=None) -> None:
        super().__init__(serde=serde)
        self._r = sync_client
        self._a = async_client
        self.p = prefix
        self.ttl = ttl_seconds

    # ----- claves -------------------------------------------------------------------------------------
    def _k_ckpt(self, t, ns, cid): return f"{self.p}:ckpt:{t}:{ns}:{cid}"
    def _k_idx(self, t, ns): return f"{self.p}:ckpt_idx:{t}:{ns}"
    def _k_ns(self, t): return f"{self.p}:ckpt_ns:{t}"
    def _k_writes(self, t, ns, cid): return f"{self.p}:writes:{t}:{ns}:{cid}"

    # ----- (de)serialización --------------------------------------------------------------------------
    def _empaquetar(self, checkpoint, metadata, parent) -> dict:
        ct, cb = self.serde.dumps_typed(checkpoint)
        mt, mb = self.serde.dumps_typed(metadata)
        return {"ct": ct, "cb": cb, "mt": mt, "mb": mb, "parent": parent or ""}

    def _tupla(self, t, ns, cid, h: dict, writes: dict, config=None) -> CheckpointTuple:
        g = lambda k: h[k.encode()] if k.encode() in h else h[k]  # noqa: E731
        checkpoint = self.serde.loads_typed((g("ct").decode(), g("cb")))
        metadata = self.serde.loads_typed((g("mt").decode(), g("mb")))
        parent = g("parent").decode()
        filas = []
        for campo, valor in writes.items():
            task_id, idx = (campo.decode() if isinstance(campo, bytes) else campo).split(SEP)
            d = json.loads(valor)
            filas.append((d["task_path"], task_id, int(idx), d["channel"], (d["type"], base64.b64decode(d["blob"]))))
        filas.sort(key=lambda f: writes_sort_key(f[0], f[1], f[2]))
        pendientes = [(tid, ch, self.serde.loads_typed(v)) for _, tid, _, ch, v in filas]
        return CheckpointTuple(
            config=config or {"configurable": {"thread_id": t, "checkpoint_ns": ns, "checkpoint_id": cid}},
            checkpoint=checkpoint, metadata=metadata, pending_writes=pendientes,
            parent_config=({"configurable": {"thread_id": t, "checkpoint_ns": ns, "checkpoint_id": parent}}
                           if parent else None))

    @staticmethod
    def _ids(config):
        c = config["configurable"]
        return c["thread_id"], c.get("checkpoint_ns", "")

    def _filas_escritura(self, config, writes, task_id, task_path):
        t, ns = self._ids(config)
        cid = config["configurable"]["checkpoint_id"]
        filas = []
        for idx, (canal, valor) in enumerate(writes):
            tipo, blob = self.serde.dumps_typed(valor)
            filas.append((WRITES_IDX_MAP.get(canal, idx), canal, tipo, blob))
        return t, ns, cid, filas, task_path

    @staticmethod
    def _campo_escritura(task_id, idx): return f"{task_id}{SEP}{idx}"

    @staticmethod
    def _valor_escritura(canal, tipo, blob, task_path):
        return json.dumps({"channel": canal, "type": tipo, "blob": base64.b64encode(blob).decode(),
                           "task_path": task_path})

    @staticmethod
    def _cumple(metadata: dict, filtro: Optional[dict]) -> bool:
        return not filtro or all(metadata.get(k) == v for k, v in filtro.items())

    # ====================================== API SÍNCRONA ================================================
    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        t, ns = self._ids(config)
        cid = get_checkpoint_id(config)
        if not cid:
            ultimo = self._r.zrange(self._k_idx(t, ns), "+", "-", bylex=True, desc=True, offset=0, num=1)
            if not ultimo:
                return None
            cid = ultimo[0].decode()
        h = self._r.hgetall(self._k_ckpt(t, ns, cid))
        if not h:
            return None
        w = self._r.hgetall(self._k_writes(t, ns, cid))
        return self._tupla(t, ns, cid, h, w, config if get_checkpoint_id(config) else None)

    def list(self, config: Optional[RunnableConfig], *, filter: Optional[dict] = None,
             before: Optional[RunnableConfig] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
        if config is None:
            raise ValueError("RedisCheckpointSaver.list requiere un config con thread_id.")
        t, ns_cfg = self._ids(config)
        ns_lista = [ns_cfg] if "checkpoint_ns" in config["configurable"] else \
            [n.decode() for n in self._r.smembers(self._k_ns(t))]
        antes = get_checkpoint_id(before) if before else None
        solo = get_checkpoint_id(config)
        restante = limit
        for ns in ns_lista:
            for cid_b in self._r.zrange(self._k_idx(t, ns), "+", "-", bylex=True, desc=True):
                cid = cid_b.decode()
                if (solo and cid != solo) or (antes and cid >= antes):
                    continue
                h = self._r.hgetall(self._k_ckpt(t, ns, cid))
                if not h:
                    continue
                tupla = self._tupla(t, ns, cid, h, self._r.hgetall(self._k_writes(t, ns, cid)))
                if not self._cumple(tupla.metadata, filter):
                    continue
                if restante is not None:
                    if restante <= 0:
                        return
                    restante -= 1
                yield tupla

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> RunnableConfig:
        t, ns = self._ids(config)
        cid = checkpoint["id"]
        datos = self._empaquetar(checkpoint, get_checkpoint_metadata(config, metadata),
                                 config["configurable"].get("checkpoint_id"))
        pipe = self._r.pipeline()
        pipe.hset(self._k_ckpt(t, ns, cid), mapping=datos)
        pipe.zadd(self._k_idx(t, ns), {cid: 0})
        pipe.sadd(self._k_ns(t), ns)
        for k in (self._k_ckpt(t, ns, cid), self._k_idx(t, ns), self._k_ns(t)):
            if self.ttl:
                pipe.expire(k, self.ttl)
        pipe.execute()
        return {"configurable": {"thread_id": t, "checkpoint_ns": ns, "checkpoint_id": cid}}

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                   task_path: str = "") -> None:
        t, ns, cid, filas, path = self._filas_escritura(config, writes, task_id, task_path)
        clave = self._k_writes(t, ns, cid)
        pipe = self._r.pipeline()
        for idx, canal, tipo, blob in filas:
            campo, valor = self._campo_escritura(task_id, idx), self._valor_escritura(canal, tipo, blob, path)
            if idx >= 0:
                pipe.hsetnx(clave, campo, valor)     # las escrituras normales no se sobrescriben
            else:
                pipe.hset(clave, campo, valor)       # canales especiales (p. ej. interrupt): se reemplazan
        if self.ttl:
            pipe.expire(clave, self.ttl)
        pipe.execute()

    def delete_thread(self, thread_id: str) -> None:
        for ns_b in self._r.smembers(self._k_ns(thread_id)):
            ns = ns_b.decode()
            claves = [self._k_idx(thread_id, ns)]
            for cid_b in self._r.zrange(self._k_idx(thread_id, ns), 0, -1):
                cid = cid_b.decode()
                claves += [self._k_ckpt(thread_id, ns, cid), self._k_writes(thread_id, ns, cid)]
            self._r.delete(*claves)
        self._r.delete(self._k_ns(thread_id))

    # ====================================== API ASÍNCRONA ===============================================
    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        t, ns = self._ids(config)
        cid = get_checkpoint_id(config)
        if not cid:
            ultimo = await self._a.zrange(self._k_idx(t, ns), "+", "-", bylex=True, desc=True, offset=0, num=1)
            if not ultimo:
                return None
            cid = ultimo[0].decode()
        h = await self._a.hgetall(self._k_ckpt(t, ns, cid))
        if not h:
            return None
        w = await self._a.hgetall(self._k_writes(t, ns, cid))
        return self._tupla(t, ns, cid, h, w, config if get_checkpoint_id(config) else None)

    async def alist(self, config: Optional[RunnableConfig], *, filter: Optional[dict] = None,
                    before: Optional[RunnableConfig] = None, limit: Optional[int] = None
                    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise ValueError("RedisCheckpointSaver.alist requiere un config con thread_id.")
        t, ns_cfg = self._ids(config)
        ns_lista = [ns_cfg] if "checkpoint_ns" in config["configurable"] else \
            [n.decode() for n in await self._a.smembers(self._k_ns(t))]
        antes = get_checkpoint_id(before) if before else None
        solo = get_checkpoint_id(config)
        restante = limit
        for ns in ns_lista:
            for cid_b in await self._a.zrange(self._k_idx(t, ns), "+", "-", bylex=True, desc=True):
                cid = cid_b.decode()
                if (solo and cid != solo) or (antes and cid >= antes):
                    continue
                h = await self._a.hgetall(self._k_ckpt(t, ns, cid))
                if not h:
                    continue
                tupla = self._tupla(t, ns, cid, h, await self._a.hgetall(self._k_writes(t, ns, cid)))
                if not self._cumple(tupla.metadata, filter):
                    continue
                if restante is not None:
                    if restante <= 0:
                        return
                    restante -= 1
                yield tupla

    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
                   new_versions: ChannelVersions) -> RunnableConfig:
        t, ns = self._ids(config)
        cid = checkpoint["id"]
        datos = self._empaquetar(checkpoint, get_checkpoint_metadata(config, metadata),
                                 config["configurable"].get("checkpoint_id"))
        pipe = self._a.pipeline()
        pipe.hset(self._k_ckpt(t, ns, cid), mapping=datos)
        pipe.zadd(self._k_idx(t, ns), {cid: 0})
        pipe.sadd(self._k_ns(t), ns)
        if self.ttl:
            for k in (self._k_ckpt(t, ns, cid), self._k_idx(t, ns), self._k_ns(t)):
                pipe.expire(k, self.ttl)
        await pipe.execute()
        return {"configurable": {"thread_id": t, "checkpoint_ns": ns, "checkpoint_id": cid}}

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                          task_path: str = "") -> None:
        t, ns, cid, filas, path = self._filas_escritura(config, writes, task_id, task_path)
        clave = self._k_writes(t, ns, cid)
        pipe = self._a.pipeline()
        for idx, canal, tipo, blob in filas:
            campo, valor = self._campo_escritura(task_id, idx), self._valor_escritura(canal, tipo, blob, path)
            if idx >= 0:
                pipe.hsetnx(clave, campo, valor)
            else:
                pipe.hset(clave, campo, valor)
        if self.ttl:
            pipe.expire(clave, self.ttl)
        await pipe.execute()

    async def adelete_thread(self, thread_id: str) -> None:
        for ns_b in await self._a.smembers(self._k_ns(thread_id)):
            ns = ns_b.decode()
            claves = [self._k_idx(thread_id, ns)]
            for cid_b in await self._a.zrange(self._k_idx(thread_id, ns), 0, -1):
                cid = cid_b.decode()
                claves += [self._k_ckpt(thread_id, ns, cid), self._k_writes(thread_id, ns, cid)]
            await self._a.delete(*claves)
        await self._a.delete(self._k_ns(thread_id))
