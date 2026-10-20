"""Configuración de la aplicación a partir de variables de entorno.

Todas las variables se leen al llamar a ``Settings.from_env()``; no hay estado global oculto,
lo que permite probar distintas configuraciones sin credenciales reales.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

RAIZ_PROYECTO = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Error de configuración (variable faltante o inválida). El mensaje es seguro de mostrar."""


def _bool(valor: Optional[str], defecto: bool = False) -> bool:
    if valor is None or valor.strip() == "":
        return defecto
    return valor.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _int(env: Mapping[str, str], nombre: str, defecto: int, minimo: int = 0) -> int:
    crudo = env.get(nombre, "").strip()
    if not crudo:
        return defecto
    try:
        valor = int(crudo)
    except ValueError as exc:
        raise ConfigError(f"La variable {nombre} debe ser un entero (valor recibido: {crudo!r}).") from exc
    if valor < minimo:
        raise ConfigError(f"La variable {nombre} debe ser >= {minimo}.")
    return valor


def _float_opcional(env: Mapping[str, str], nombre: str) -> Optional[float]:
    crudo = env.get(nombre, "").strip()
    if not crudo:
        return None
    try:
        valor = float(crudo)
    except ValueError as exc:
        raise ConfigError(f"La variable {nombre} debe ser numérica (valor recibido: {crudo!r}).") from exc
    if valor < 0:
        raise ConfigError(f"La variable {nombre} no puede ser negativa.")
    return valor


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "l10n"

    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_model: str = ""
    openai_timeout: int = 60
    openai_max_retries: int = 2
    # Tarifas por millón de tokens (USD). Si no se configuran, el costo se informa como «desconocido».
    openai_price_input_per_1m: Optional[float] = None
    openai_price_output_per_1m: Optional[float] = None

    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "l10n-qa-orchestrator"

    approval_token: str = ""
    worker_concurrency: int = 3
    max_attempts: int = 3
    lease_seconds: int = 120
    poll_interval: float = 0.2
    max_steps: int = 12
    task_ttl_seconds: int = 7 * 24 * 3600
    embedded_worker: bool = False

    input_root: Path = RAIZ_PROYECTO / "fixtures"
    output_dir: Path = RAIZ_PROYECTO / "salida"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        tracing_crudo = env.get("LANGSMITH_TRACING") or env.get("LANGCHAIN_TRACING_V2")
        return cls(
            app_env=env.get("APP_ENV", "local"),
            redis_url=env.get("REDIS_URL", "redis://localhost:6379/0"),
            redis_prefix=env.get("REDIS_PREFIX", "l10n"),
            llm_provider=(env.get("LLM_PROVIDER", "openai") or "openai").strip().lower(),
            openai_api_key=env.get("OPENAI_API_KEY", "").strip(),
            openai_model=env.get("OPENAI_MODEL", "").strip(),
            openai_timeout=_int(env, "OPENAI_TIMEOUT", 60, 1),
            openai_max_retries=_int(env, "OPENAI_MAX_RETRIES", 2, 0),
            openai_price_input_per_1m=_float_opcional(env, "OPENAI_PRICE_INPUT_PER_1M"),
            openai_price_output_per_1m=_float_opcional(env, "OPENAI_PRICE_OUTPUT_PER_1M"),
            langsmith_tracing=_bool(tracing_crudo),
            langsmith_api_key=(env.get("LANGSMITH_API_KEY") or env.get("LANGCHAIN_API_KEY") or "").strip(),
            langsmith_project=env.get("LANGSMITH_PROJECT") or env.get("LANGCHAIN_PROJECT") or "l10n-qa-orchestrator",
            approval_token=env.get("APPROVAL_TOKEN", "").strip(),
            worker_concurrency=_int(env, "WORKER_CONCURRENCY", 3, 1),
            max_attempts=_int(env, "MAX_ATTEMPTS", 3, 1),
            lease_seconds=_int(env, "LEASE_SECONDS", 120, 5),
            poll_interval=float(env.get("QUEUE_POLL_INTERVAL", "0.2") or 0.2),
            max_steps=_int(env, "L10N_QA_MAX_STEPS", 12, 4),
            task_ttl_seconds=_int(env, "TASK_TTL_SECONDS", 7 * 24 * 3600, 60),
            embedded_worker=_bool(env.get("EMBEDDED_WORKER")),
            input_root=Path(env.get("INPUT_ROOT") or RAIZ_PROYECTO / "fixtures").resolve(),
            output_dir=Path(env.get("OUTPUT_DIR") or RAIZ_PROYECTO / "salida").resolve(),
        )
