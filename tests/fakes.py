"""Dobles de prueba para el LLM. Son claramente falsos: nunca se presentan como llamadas reales a OpenAI."""
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.llm.factory import ManejadorLLM


class ChatFalso(BaseChatModel):
    respuesta: str = "Resumen ejecutivo de prueba."
    uso: Optional[dict] = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    error: Optional[Any] = None
    llamadas: list = []

    @property
    def _llm_type(self) -> str:
        return "falso"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.llamadas.append(messages)
        if self.error:
            raise self.error
        msg = AIMessage(content=self.respuesta, usage_metadata=self.uso, response_metadata={"model_name": "falso-1"})
        return ChatResult(generations=[ChatGeneration(message=msg)])


def handle_falso(**kw) -> ManejadorLLM:
    return ManejadorLLM("falso", "falso-1", True, ChatFalso(llamadas=[], **kw))
