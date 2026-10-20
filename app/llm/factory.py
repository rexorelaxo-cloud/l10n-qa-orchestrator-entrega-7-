"""Fábrica de modelos de lenguaje desacoplada del proveedor.

Agregar un proveedor nuevo no requiere tocar los agentes ni el grafo: basta con implementar
``ProveedorLLM`` y registrarlo con ``registrar_proveedor``. Hoy existen dos proveedores:

* ``openai``: proveedor real (``langchain-openai``). Requiere ``OPENAI_API_KEY`` y ``OPENAI_MODEL``.
* ``local``: modo determinista SIN modelo. No realiza llamadas a ningún LLM ni las simula:
  ``ManejadorLLM.es_real`` es ``False`` y los nodos lo informan explícitamente en sus resultados.

No hay fallback automático entre proveedores: el proveedor se elige de forma explícita.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from app.config import ConfigError, Settings


@dataclass
class ManejadorLLM:
    """Resultado de la fábrica: lo único que los agentes conocen del proveedor."""

    proveedor: str
    modelo: str
    es_real: bool
    modelo_chat: Optional[Any] = None   # BaseChatModel de LangChain cuando es_real


class ProveedorLLM(Protocol):
    nombre: str

    def validar(self, settings: Settings) -> None:
        """Lanza ``ConfigError`` con un mensaje claro si falta configuración."""

    def crear(self, settings: Settings) -> ManejadorLLM: ...


class ProveedorOpenAI:
    nombre = "openai"

    def validar(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ConfigError(
                "LLM_PROVIDER=openai requiere la variable OPENAI_API_KEY. "
                "Defínala o use LLM_PROVIDER=local para el modo determinista sin LLM."
            )
        if not settings.openai_model:
            raise ConfigError(
                "LLM_PROVIDER=openai requiere la variable OPENAI_MODEL (nombre del modelo de OpenAI a usar)."
            )

    def crear(self, settings: Settings) -> ManejadorLLM:
        self.validar(settings)
        from langchain_openai import ChatOpenAI  # import diferido: el modo local no lo necesita

        modelo = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0,
            timeout=settings.openai_timeout,
            max_retries=settings.openai_max_retries,
        )
        return ManejadorLLM("openai", settings.openai_model, True, modelo)


class ProveedorLocal:
    nombre = "local"

    def validar(self, settings: Settings) -> None:  # no requiere credenciales
        return None

    def crear(self, settings: Settings) -> ManejadorLLM:
        return ManejadorLLM("local", "ninguno (determinista)", False, None)


_REGISTRO: dict[str, ProveedorLLM] = {}


def registrar_proveedor(proveedor: ProveedorLLM) -> None:
    _REGISTRO[proveedor.nombre] = proveedor


registrar_proveedor(ProveedorOpenAI())
registrar_proveedor(ProveedorLocal())


def proveedores_disponibles() -> list[str]:
    return sorted(_REGISTRO)


def _proveedor(settings: Settings) -> ProveedorLLM:
    try:
        return _REGISTRO[settings.llm_provider]
    except KeyError:
        raise ConfigError(
            f"LLM_PROVIDER={settings.llm_provider!r} no está implementado. "
            f"Proveedores disponibles: {', '.join(proveedores_disponibles())}."
        ) from None


def validar_configuracion(settings: Settings) -> None:
    """Valida el proveedor elegido sin crear el modelo (se usa al aceptar una tarea)."""
    _proveedor(settings).validar(settings)


def crear_llm(settings: Optional[Settings] = None) -> ManejadorLLM:
    settings = settings or Settings.from_env()
    return _proveedor(settings).crear(settings)


def costo_estimado(settings: Settings, tokens_entrada: Optional[int], tokens_salida: Optional[int]) -> dict:
    """Costo calculado a partir de tarifas CONFIGURADAS por el usuario; nunca se inventan precios."""
    pi, po = settings.openai_price_input_per_1m, settings.openai_price_output_per_1m
    if None in (pi, po) or None in (tokens_entrada, tokens_salida):
        return {"origen": "desconocido", "usd": None,
                "nota": "Defina OPENAI_PRICE_INPUT_PER_1M y OPENAI_PRICE_OUTPUT_PER_1M para estimar el costo."}
    usd = tokens_entrada * pi / 1_000_000 + tokens_salida * po / 1_000_000
    return {"origen": "estimado_con_tarifa_configurada", "usd": round(usd, 8),
            "tarifa_entrada_por_1m": pi, "tarifa_salida_por_1m": po}
