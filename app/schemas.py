"""Esquemas Pydantic de la API y estados de las tareas."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    DONE = "DONE"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


ESTADOS_FINALES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.REJECTED}

# Transiciones válidas del estado operativo (cualquier otra se rechaza):
TRANSICIONES_VALIDAS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.RUNNING: {TaskStatus.RUNNING,            # latido (renovación de la reserva)
                         TaskStatus.PENDING,            # reintento o recuperación tras una interrupción
                         TaskStatus.WAITING_APPROVAL, TaskStatus.DONE, TaskStatus.FAILED},
    TaskStatus.WAITING_APPROVAL: {TaskStatus.PENDING,   # decisión humana registrada → se encola la reanudación
                                  TaskStatus.FAILED},
    TaskStatus.DONE: set(), TaskStatus.FAILED: set(), TaskStatus.REJECTED: set(),
}
TRANSICIONES_VALIDAS[TaskStatus.RUNNING] |= {TaskStatus.REJECTED}


def iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "request": "Compará los JSON de origen con sus traducciones y detectá problemas estructurales.",
        "source_paths": ["source/clean.en.json"], "target_paths": ["targets/clean.es.json"],
        "max_steps": 12, "require_approval": True}})

    request: str = Field(..., min_length=1, max_length=4000,
                         description="Solicitud de QA en lenguaje natural.")
    source_paths: list[str] = Field(..., min_length=1, max_length=50,
                                    description="Archivos o carpetas de origen (relativos a INPUT_ROOT).")
    target_paths: list[str] = Field(..., min_length=1, max_length=50,
                                    description="Archivos o carpetas traducidos (relativos a INPUT_ROOT).")
    max_steps: Optional[int] = Field(None, ge=4, le=50, description="Límite de pasos del grafo.")
    require_approval: bool = Field(True, description="Si es true, la preparación del paquete de importación "
                                                     "al CMS (simulada) requiere aprobación humana.")

    @field_validator("request")
    @classmethod
    def _no_vacia(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("La solicitud no puede estar vacía.")
        return v

    @field_validator("source_paths", "target_paths")
    @classmethod
    def _rutas(cls, v: list[str]) -> list[str]:
        if any(not r.strip() or "\x00" in r for r in v):
            raise ValueError("Las rutas no pueden estar vacías ni contener caracteres nulos.")
        return v


class TaskCreatedResponse(BaseModel):
    job_id: str
    thread_id: str
    status: TaskStatus
    status_url: str = Field(..., description="URL para consultar el estado y el resultado.")
    enqueue_ms: float = Field(..., description="Tiempo que tardó el servidor en validar, persistir y encolar.")


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "decision": "approve", "approver": "diego.farias", "comment": "Revisado, se puede importar."}})
    decision: Literal["approve", "reject"] = Field(..., description="Decisión explícita.")
    approver: str = Field(..., min_length=1, max_length=100, description="Identificador o nombre de quien decide.")
    comment: Optional[str] = Field(None, max_length=1000)


class ApprovalResponse(BaseModel):
    job_id: str
    status: TaskStatus
    decision: Literal["approve", "reject"]
    approver: str
    decided_at: str
    message: str


class TaskView(BaseModel):
    job_id: str
    thread_id: str
    status: TaskStatus
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    approval_request: Optional[dict[str, Any]] = Field(None, description="Qué se está pidiendo aprobar (solo en WAITING_APPROVAL).")
    approval: Optional[dict[str, Any]] = Field(None, description="Decisión humana registrada.")
    result: Optional[dict[str, Any]] = None
    metrics: Optional[dict[str, Any]] = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    redis: bool
    llm_provider: str
    llm_configured: bool
    langsmith_tracing: bool
    queue_pending: Optional[int] = None
    version: str = "1.0.0"
