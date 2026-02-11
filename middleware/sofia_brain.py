# middleware/sofia_brain.py
"""
Cerebro de Sofía - Motor de IA con Memoria.

- LangChain para orquestación
- LLM de OpenAI gpt-4o-mini
- Redis para memoria de conversación (últimos 10-15 mensajes)

Sofía NO puede "olvidar" lo que se dijo hace 2 minutos.
La memoria persiste en Redis con TTL de 24 horas.
"""

import os
from typing import Optional, Dict, Any

from langchain_openai import ChatOpenAI
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.messages import HumanMessage, AIMessage

from logging_config import logger
from prompts.middleware_prompts import (
    SOFIA_MIDDLEWARE_SYSTEM_PROMPT,
    HANDOFF_KEYWORDS,
    MIDDLEWARE_MESSAGES,
)


class SofiaBrain:
    """
    Cerebro de Sofía con memoria persistente.

    Características:
    - Memoria de últimos 10-15 mensajes por conversación
    - Usa OpenAI gpt-4o-mini
    - Integración con perfil de HubSpot para contexto adicional
    - TTL de 24 horas en Redis
    """

    # Número máximo de mensajes a mantener en memoria
    MAX_HISTORY_MESSAGES = 15

    # TTL de la sesión en segundos (24 horas)
    SESSION_TTL = 24 * 60 * 60

    def __init__(
        self,
        redis_url: str,
        openai_api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        temperature: float = 0.3,
    ):
        """
        Inicializa el cerebro de Sofía.

        Args:
            redis_url: URL de conexión a Redis
            openai_api_key: API key de OpenAI (opcional, usa env var)
            model: Modelo de OpenAI a usar (default: gpt-4o-mini)
            temperature: Temperatura del modelo (default: 0.3)
        """
        self.redis_url = redis_url

        # Inicializar LLM
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY no configurada.")

        self.llm = ChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=500,
        )

        # Configurar prompt usando el prompt centralizado
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", SOFIA_MIDDLEWARE_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ])

        # Crear cadena base
        self.chain = self.prompt | self.llm

        logger.info(f"[SofiaBrain] Inicializado con OpenAI {model}")

    def _get_message_history(self, session_id: str) -> RedisChatMessageHistory:
        """
        Obtiene el historial de mensajes de Redis.

        Args:
            session_id: ID de sesión (número de teléfono normalizado)

        Returns:
            RedisChatMessageHistory configurado
        """
        return RedisChatMessageHistory(
            session_id=session_id,
            url=self.redis_url,
            key_prefix="message_store:",
            ttl=self.SESSION_TTL,
        )

    async def process_message(
        self,
        session_id: str,
        user_message: str,
        lead_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Procesa un mensaje del usuario y genera respuesta.

        Este es el método principal del cerebro de Sofía.

        Args:
            session_id: ID de sesión (número normalizado, ej: "+573001234567")
            user_message: Mensaje del usuario
            lead_context: Contexto adicional del lead desde HubSpot (opcional)

        Returns:
            Respuesta generada por Sofía
        """
        logger.info(f"[SofiaBrain] Procesando mensaje de {session_id}")

        # Crear runnable con historial
        with_message_history = RunnableWithMessageHistory(
            self.chain,
            self._get_message_history,
            input_messages_key="input",
            history_messages_key="history",
        )

        # Preparar input
        input_data = {"input": user_message}

        # Agregar contexto del lead si existe
        if lead_context:
            context_str = self._format_lead_context(lead_context)
            input_data["input"] = f"{context_str}\n\nMensaje del cliente: {user_message}"

        # Configuración de sesión
        config = {"configurable": {"session_id": session_id}}

        try:
            # Invocar cadena
            response = with_message_history.invoke(input_data, config=config)

            # Extraer contenido de la respuesta
            if hasattr(response, "content"):
                response_text = response.content
            else:
                response_text = str(response)

            logger.info(f"[SofiaBrain] Respuesta generada para {session_id}")

            # Truncar historial si excede el máximo
            await self._trim_history(session_id)

            return response_text

        except Exception as e:
            logger.error(f"[SofiaBrain] Error procesando mensaje: {e}", exc_info=True)
            return MIDDLEWARE_MESSAGES["error_processing"]

    def _format_lead_context(self, lead_context: Dict[str, Any]) -> str:
        """
        Formatea el contexto del lead para incluir en el prompt.

        Args:
            lead_context: Datos del lead desde HubSpot

        Returns:
            String formateado con el contexto
        """
        parts = ["[Contexto del cliente - NO menciones esto directamente:]"]

        if lead_context.get("firstname"):
            parts.append(f"- Nombre: {lead_context['firstname']}")

        if lead_context.get("chatbot_property_type"):
            parts.append(f"- Interesado en: {lead_context['chatbot_property_type']}")

        if lead_context.get("chatbot_operation_type"):
            parts.append(f"- Operación: {lead_context['chatbot_operation_type']}")

        if lead_context.get("chatbot_location"):
            parts.append(f"- Zona preferida: {lead_context['chatbot_location']}")

        if lead_context.get("chatbot_budget"):
            parts.append(f"- Presupuesto: {lead_context['chatbot_budget']}")

        return "\n".join(parts)

    async def _trim_history(self, session_id: str) -> None:
        """
        Recorta el historial si excede el máximo de mensajes.

        Args:
            session_id: ID de sesión
        """
        try:
            history = self._get_message_history(session_id)
            messages = history.messages

            if len(messages) > self.MAX_HISTORY_MESSAGES:
                # Mantener solo los últimos N mensajes
                messages_to_keep = messages[-self.MAX_HISTORY_MESSAGES:]

                # Limpiar y re-agregar
                history.clear()
                for msg in messages_to_keep:
                    history.add_message(msg)

                logger.debug(
                    f"[SofiaBrain] Historial recortado para {session_id}: "
                    f"{len(messages)} → {len(messages_to_keep)}"
                )

        except Exception as e:
            logger.warning(f"[SofiaBrain] Error recortando historial: {e}")

    async def get_conversation_summary(self, session_id: str) -> str:
        """
        Genera un resumen de la conversación para HubSpot.

        Args:
            session_id: ID de sesión

        Returns:
            Resumen de la conversación
        """
        try:
            history = self._get_message_history(session_id)
            messages = history.messages

            if not messages:
                return "Sin conversación registrada."

            # Formatear mensajes
            lines = []
            for msg in messages[-10:]:  # Últimos 10 para el resumen
                if isinstance(msg, HumanMessage):
                    lines.append(f"Cliente: {msg.content}")
                elif isinstance(msg, AIMessage):
                    lines.append(f"Sofía: {msg.content}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[SofiaBrain] Error obteniendo resumen: {e}")
            return "Error al obtener resumen de conversación."

    async def clear_history(self, session_id: str) -> None:
        """
        Limpia el historial de una conversación.

        Args:
            session_id: ID de sesión
        """
        try:
            history = self._get_message_history(session_id)
            history.clear()
            logger.info(f"[SofiaBrain] Historial limpiado para {session_id}")
        except Exception as e:
            logger.error(f"[SofiaBrain] Error limpiando historial: {e}")

    def detect_handoff_intent(self, message: str) -> bool:
        """
        Detecta si el mensaje indica intención de hablar con humano.

        Args:
            message: Mensaje del usuario

        Returns:
            True si se detecta intención de handoff
        """
        message_lower = message.lower()
        return any(keyword in message_lower for keyword in HANDOFF_KEYWORDS)