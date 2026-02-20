# middleware/sofia_brain.py
"""
Cerebro de Sofía - Motor de IA con Memoria.
"""

import os
import json
from typing import Optional, Dict, Any
from dataclasses import dataclass, field, asdict

from langchain_openai import ChatOpenAI
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.messages import HumanMessage, AIMessage

from logging_config import logger
from prompts.middleware_prompts import (
    SOFIA_MIDDLEWARE_SYSTEM_PROMPT,
    SOFIA_SINGLE_STREAM_SYSTEM_PROMPT,
    HANDOFF_KEYWORDS,
    MIDDLEWARE_MESSAGES,
)


# ═══════════════════════════════════════════════════════════════════════════════
# ESTRUCTURAS DE DATOS PARA ANÁLISIS SINGLE-STREAM
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MessageAnalysis:
    """Análisis del mensaje del cliente."""
    emocion: str = "neutral"
    sentiment_score: int = 5
    intencion_visita: bool = False
    pregunta_tecnica: bool = False
    handoff_priority: str = "none"
    link_redes_sociales: bool = False  # True si envió link de Instagram/Facebook/TikTok
    suspicious_indicators: list = None  # Lista de indicadores sospechosos
    summary_update: Optional[str] = None
    # Campos para detección de citas
    fecha_cita_mencionada: Optional[str] = None  # ISO format (YYYY-MM-DD)
    hora_cita_mencionada: Optional[str] = None   # HH:MM format
    cita_confirmada: bool = False  # True si el cliente confirma una cita propuesta

    def __post_init__(self):
        if self.suspicious_indicators is None:
            self.suspicious_indicators = []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageAnalysis":
        return cls(
            emocion=data.get("emocion", "neutral"),
            sentiment_score=data.get("sentiment_score", 5),
            intencion_visita=data.get("intencion_visita", False),
            pregunta_tecnica=data.get("pregunta_tecnica", False),
            handoff_priority=data.get("handoff_priority", "none"),
            link_redes_sociales=data.get("link_redes_sociales", False),
            suspicious_indicators=data.get("suspicious_indicators", []),
            summary_update=data.get("summary_update"),
            fecha_cita_mencionada=data.get("fecha_cita_mencionada"),
            hora_cita_mencionada=data.get("hora_cita_mencionada"),
            cita_confirmada=data.get("cita_confirmada", False)
        )


@dataclass
class SingleStreamResponse:
    """Respuesta completa del procesamiento Single-Stream."""
    respuesta: str
    analisis: MessageAnalysis
    raw_json: Optional[Dict] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "respuesta": self.respuesta,
            "analisis": self.analisis.to_dict(),
        }


class SofiaBrain:
    """
    Cerebro de Sofía con memoria persistente.

    SEGREGACIÓN POR CANAL:
    El session_id ahora incluye el canal de origen para evitar que
    el mismo teléfono desde diferentes portales comparta historial de chat.

    Formato: {phone}:{canal}
    Ejemplo: +573001234567:instagram
    """

    # Número máximo de mensajes a mantener en memoria
    MAX_HISTORY_MESSAGES = 15

    # TTL de la sesión en segundos (24 horas)
    SESSION_TTL = 24 * 60 * 60

    # Canal por defecto para compatibilidad
    DEFAULT_CANAL = "default"

    def __init__(
        self,
        redis_url: str,
        openai_api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        temperature: float = 0.3,
        use_single_stream: bool = True,
    ):
        """
        Inicializa el cerebro de Sofía.
        """
        self.redis_url = redis_url
        self.use_single_stream = use_single_stream

        # Inicializar LLM
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY no configurada.")

        self.llm = ChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=800,  # Aumentado para incluir JSON de análisis
        )

        # Configurar prompt usando el prompt centralizado
        # Usar Single-Stream si está habilitado
        system_prompt = SOFIA_SINGLE_STREAM_SYSTEM_PROMPT if use_single_stream else SOFIA_MIDDLEWARE_SYSTEM_PROMPT

        self.prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ])

        # Crear cadena base
        self.chain = self.prompt | self.llm

        mode = "Single-Stream" if use_single_stream else "Legacy"
        logger.info(f"[SofiaBrain] Inicializado con OpenAI {model} (modo: {mode})")

    def _build_session_id(self, phone: str, canal: Optional[str] = None) -> str:
        """
        Construye el session_id con segregación por canal.

        Args:
            phone: Número de teléfono normalizado
            canal: Canal de origen (instagram, finca_raiz, etc.)

        Returns:
            Session ID en formato phone:canal
        """
        canal_safe = canal or self.DEFAULT_CANAL
        return f"{phone}:{canal_safe}"

    def _get_message_history(self, session_id: str) -> RedisChatMessageHistory:
        """
        Obtiene el historial de mensajes de Redis.

        Args:
            session_id: ID de sesión (formato phone:canal)

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
        lead_context: Optional[Dict[str, Any]] = None,
        canal: Optional[str] = None
    ) -> str:
        """
        Procesa un mensaje del usuario y genera respuesta.

        Este es el método principal del cerebro de Sofía.
        Si use_single_stream está habilitado, usa internamente process_message_with_analysis
        y retorna solo la respuesta.

        Args:
            session_id: ID de sesión (teléfono normalizado)
            user_message: Mensaje del usuario
            lead_context: Contexto adicional del lead
            canal: Canal de origen para segregación de historial
        """
        # Construir session_id con canal para segregación
        composite_session_id = self._build_session_id(session_id, canal)

        if self.use_single_stream:
            # Usar Single-Stream y extraer solo la respuesta
            result = await self.process_message_with_analysis(
                session_id=composite_session_id,
                user_message=user_message,
                lead_context=lead_context
            )
            return result.respuesta

        # Modo legacy: proceso original sin análisis
        return await self._process_message_legacy(composite_session_id, user_message, lead_context)

    async def _process_message_legacy(
        self,
        session_id: str,
        user_message: str,
        lead_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Procesa mensaje en modo legacy (sin análisis).

        Args:
            session_id: ID de sesión
            user_message: Mensaje del usuario
            lead_context: Contexto adicional

        Returns:
            Respuesta generada por Sofía
        """
        logger.info(f"[SofiaBrain] Procesando mensaje de {session_id} (modo legacy)")

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

    async def process_message_with_analysis(
        self,
        session_id: str,
        user_message: str,
        lead_context: Optional[Dict[str, Any]] = None
    ) -> SingleStreamResponse:
        """
        Procesa un mensaje y retorna respuesta + análisis en una sola llamada LLM.
        """
        logger.info(f"[SofiaBrain] Procesando mensaje Single-Stream de {session_id}")

        # Crear runnable con historial
        with_message_history = RunnableWithMessageHistory(
            self.chain,
            self._get_message_history,
            input_messages_key="input",
            history_messages_key="history",
        )

        # Verificar si hay historial previo (para evitar presentación repetida)
        history = self._get_message_history(session_id)
        has_previous_messages = len(history.messages) > 0

        # Preparar input
        input_data = {"input": user_message}

        # Agregar contexto del lead si existe
        if lead_context:
            context_str = self._format_lead_context(lead_context)
            input_data["input"] = f"{context_str}\n\nMensaje del cliente: {user_message}"

        # Agregar flag de historial si ya hay conversación previa
        if has_previous_messages:
            history_flag = (
                "\n\n[IMPORTANTE - CONTEXTO DE HISTORIAL]:\n"
                "Ya te presentaste como Sofía en mensajes anteriores. "
                "NO te vuelvas a presentar ni a saludar como si fuera la primera vez. "
                "Continúa la conversación de forma natural."
            )
            input_data["input"] = history_flag + "\n" + input_data["input"]

        # Configuración de sesión
        config = {"configurable": {"session_id": session_id}}

        try:
            # Invocar cadena
            response = with_message_history.invoke(input_data, config=config)

            # Extraer contenido de la respuesta
            if hasattr(response, "content"):
                raw_content = response.content
            else:
                raw_content = str(response)

            # Parsear JSON de la respuesta
            parsed = self._parse_single_stream_response(raw_content)

            logger.info(
                f"[SofiaBrain] Single-Stream completado para {session_id} | "
                f"Emoción: {parsed.analisis.emocion}, "
                f"Score: {parsed.analisis.sentiment_score}, "
                f"Handoff: {parsed.analisis.handoff_priority}"
            )

            # Truncar historial si excede el máximo
            await self._trim_history(session_id)

            return parsed

        except Exception as e:
            logger.error(f"[SofiaBrain] Error en Single-Stream: {e}", exc_info=True)
            # Retornar respuesta de error con análisis por defecto
            return SingleStreamResponse(
                respuesta=MIDDLEWARE_MESSAGES["error_processing"],
                analisis=MessageAnalysis()
            )

    def _parse_single_stream_response(self, raw_content: str) -> SingleStreamResponse:
        """
        Parsea la respuesta JSON del LLM Single-Stream.

        Args:
            raw_content: Contenido crudo de la respuesta del LLM

        Returns:
            SingleStreamResponse parseado
        """
        try:
            # Intentar parsear como JSON directo
            # El LLM puede envolver en ```json``` o no
            content = raw_content.strip()

            # Remover bloques de código markdown si existen
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            content = content.strip()

            # Parsear JSON
            data = json.loads(content)

            # Extraer respuesta y análisis
            respuesta = data.get("respuesta", "")
            analisis_data = data.get("analisis", {})

            return SingleStreamResponse(
                respuesta=respuesta,
                analisis=MessageAnalysis.from_dict(analisis_data),
                raw_json=data
            )

        except json.JSONDecodeError as e:
            logger.warning(
                f"[SofiaBrain] Error parseando JSON Single-Stream: {e}. "
                f"Usando respuesta raw."
            )
            # Si falla el parseo, usar el contenido como respuesta simple
            return SingleStreamResponse(
                respuesta=raw_content,
                analisis=MessageAnalysis()
            )

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

        # Contexto especial para links de redes sociales
        canal_origen = lead_context.get("canal_origen", "")
        if canal_origen in ["instagram", "facebook", "tiktok"]:
            parts.append("")
            parts.append("[INSTRUCCIÓN ESPECIAL - LINK DE RED SOCIAL]:")
            parts.append("El cliente llegó enviando un link de un inmueble desde redes sociales.")
            parts.append("YA tienes la información del inmueble del link.")
            parts.append("Solo necesitas pedirle su NOMBRE para conectarlo con un asesor.")
            parts.append("NO preguntes por tipo de inmueble, zona, presupuesto ni características.")
            parts.append("NO le pidas más información sobre el inmueble.")

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

    async def get_conversation_summary(
        self,
        session_id: str,
        canal: Optional[str] = None
    ) -> str:
        """
        Genera un resumen de la conversación para HubSpot.

        Args:
            session_id: ID de sesión (teléfono)
            canal: Canal de origen para segregación

        Returns:
            Resumen de la conversación
        """
        try:
            # Construir session_id con canal
            composite_session_id = self._build_session_id(session_id, canal)
            history = self._get_message_history(composite_session_id)
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

    async def clear_history(
        self,
        session_id: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Limpia el historial de una conversación.

        Args:
            session_id: ID de sesión (teléfono)
            canal: Canal de origen para segregación
        """
        try:
            # Construir session_id con canal
            composite_session_id = self._build_session_id(session_id, canal)
            history = self._get_message_history(composite_session_id)
            history.clear()
            logger.info(f"[SofiaBrain] Historial limpiado para {composite_session_id}")
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