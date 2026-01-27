# -*- coding: utf-8 -*-
# Agents/CRMAgent/crm_agent.py
"""
Agente de CRM conversacional con integración a HubSpot API v3.
Conversa con el cliente para recopilar datos antes de sincronizar con HubSpot.
"""
from datetime import datetime, timezone
from typing import Dict, Any
from state_manager import ConversationState, ConversationStatus
from integrations.hubspot.hubspot_client import HubSpotClient
from integrations.hubspot.hubspot_utils import (
    normalize_phone_e164,
    calculate_lead_score,
    split_full_name,
    format_conversation_history
)
from prompts.crm_prompts import (
    CRM_SYSTEM_PROMPT,
    CRM_CONFIRMATION_TEMPLATE,
    PROPERTY_EXTRACTION_PROMPT,
    LINK_ARRIVAL_CONTEXT
)
from integrations.hubspot.lead_assigner import lead_assigner, orphan_alert_system
from llm_client import llama_client
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from logging_config import logger
import json


class CRMAgent:
    """
    Agente conversacional que recopila datos del lead mediante LLM
    y sincroniza con HubSpot CRM cuando tiene la información necesaria.
    """

    def __init__(self):
        """Inicializa el agente con cliente HubSpot y asignador de leads."""
        self.hubspot = HubSpotClient()
        self.assigner = lead_assigner
        logger.info("[CRMAgent] Inicializado con integración HubSpot y LLM conversacional.")

    async def process_conversation(self, user_input: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa un turno de conversación con el cliente para recopilar datos.
        Usa LLM con CRM_SYSTEM_PROMPT para conversar naturalmente.
        IMPORTANTE: Siempre conversa al menos una vez antes de enviar al CRM.
        """
        try:
            logger.info(f"[CRMAgent] Procesando conversación. Input: '{user_input[:50]}...'")

            # Verificar si es primera interacción llegando por link
            if (state.metadata.get("llegada_por_link") and
                    not state.metadata.get("link_procesado")):
                return self._handle_link_arrival(user_input, state)

            crm_history = state.lead_data.get('crm_history', [])
            is_first_turn = len(crm_history) < 2  # Primer turno si no hay historial previo

            # 1. Extraer entidades del mensaje actual
            new_entities = self._extract_entities(user_input)
            metadata = state.lead_data.get('metadata', {})

            # Fusionar nuevas entidades con las existentes
            for key, value in new_entities.items():
                if value and str(value).strip():
                    metadata[key] = value

            state.lead_data['metadata'] = metadata
            logger.info(f"[CRMAgent] Metadata acumulada: {metadata}")

            # 2. En el primer turno, SIEMPRE generar respuesta conversacional
            # No intentamos extraer nombre ni enviar al CRM en el primer turno
            if is_first_turn:
                logger.info("[CRMAgent] Primer turno - generando respuesta conversacional")
                response_text = self._generate_conversation_response(user_input, state)
                return {
                    "response": response_text,
                    "new_state": state,
                    "ready_for_handoff": False
                }

            # 3. A partir del segundo turno, intentar extraer nombre
            extracted_name = self._extract_name_from_message(user_input, state)
            if extracted_name:
                state.lead_data['name'] = extracted_name
                logger.info(f"[CRMAgent] Nombre detectado: {extracted_name}")

            # 4. Verificar si estamos listos para registrar (tenemos nombre)
            lead_name = state.lead_data.get('name')
            if lead_name:
                # Tenemos nombre → enviar al CRM
                logger.info(f"[CRMAgent] Datos completos. Procediendo con registro en HubSpot...")
                result = await self.process_lead_handoff(user_input, state)
                return result

            # 5. Si no tenemos nombre, continuar conversación con LLM
            response_text = self._generate_conversation_response(user_input, state)

            return {
                "response": response_text,
                "new_state": state,
                "ready_for_handoff": False
            }

        except Exception as e:
            logger.error(f"[CRMAgent] Error en conversación: {e}", exc_info=True)
            return {
                "response": "Disculpa, tuve un inconveniente. ¿Podrías repetirme eso?",
                "new_state": state,
                "ready_for_handoff": False
            }

    def _generate_conversation_response(self, user_input: str, state: ConversationState) -> str:
        """
        Genera una respuesta conversacional usando LLM con el system prompt del CRM.
        Inyecta el historial de conversación y la metadata recopilada hasta ahora.
        """
        # Construir contexto con datos ya recopilados
        metadata = state.lead_data.get('metadata', {})
        context_info = self._build_context_summary(metadata)

        system_content = CRM_SYSTEM_PROMPT
        if context_info:
            system_content += f"\n\nDATOS YA RECOPILADOS DEL CLIENTE:\n{context_info}\nNo vuelvas a preguntar por estos datos."

        messages = [SystemMessage(content=system_content)]

        # Inyectar historial de conversación CRM (últimos 10 turnos)
        crm_history = state.lead_data.get('crm_history', [])
        for entry in crm_history[-10:]:
            if entry.startswith("User:"):
                messages.append(HumanMessage(content=entry[5:].strip()))
            elif entry.startswith("Agent:"):
                messages.append(AIMessage(content=entry[6:].strip()))

        # Agregar mensaje actual
        messages.append(HumanMessage(content=user_input))

        # Invocar LLM
        response = llama_client.invoke(messages)
        response_text = response.content.strip()

        # Guardar en historial CRM específico
        if 'crm_history' not in state.lead_data:
            state.lead_data['crm_history'] = []
        state.lead_data['crm_history'].append(f"User: {user_input}")
        state.lead_data['crm_history'].append(f"Agent: {response_text}")

        return response_text

    def _build_context_summary(self, metadata: Dict[str, Any]) -> str:
        """
        Construye un resumen legible de los datos ya recopilados.
        """
        labels = {
            "tipo_propiedad": "Tipo de propiedad",
            "tipo_operacion": "Operación",
            "ubicacion": "Zona/Barrio",
            "presupuesto": "Presupuesto",
            "caracteristicas": "Características",
            "correo": "Email",
            "tiempo": "Plazo",
            "comentarios_adicionales": "Notas"
        }

        parts = []
        for key, label in labels.items():
            value = metadata.get(key)
            if value and str(value).strip():
                parts.append(f"- {label}: {value}")

        return "\n".join(parts) if parts else ""

    def _handle_link_arrival(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Maneja la primera respuesta cuando el cliente llega enviando un link.
        """
        nombre_portal = state.metadata.get("canal_origen", "internet")
        url = state.metadata.get("url_referencia", "")

        # Obtener nombre amigable del portal
        portal_nombres = {
            "instagram": "Instagram",
            "facebook": "Facebook",
            "finca_raiz": "Finca Raíz",
            "metrocuadrado": "Metrocuadrado",
            "mercado_libre": "Mercado Libre",
            "ciencuadras": "Ciencuadras",
            "pagina_web": "nuestra página web",
            "whatsapp_direct": "WhatsApp",
            "desconocido": "internet",
        }
        nombre_portal_amigable = portal_nombres.get(nombre_portal, "internet")

        # Construir prompt con contexto del link
        link_context = LINK_ARRIVAL_CONTEXT.format(
            nombre_portal=nombre_portal_amigable,
            url_referencia=url
        )

        # Construir system prompt completo
        system_content = CRM_SYSTEM_PROMPT + "\n\n" + link_context

        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content=message)
        ]

        # Generar respuesta personalizada
        response = llama_client.invoke(messages)
        response_text = response.content.strip()

        # Marcar como procesado para no repetir
        state.metadata["link_procesado"] = True

        # Intentar extraer entidades del link/mensaje
        entities = self._extract_entities_from_link(url, message)
        if entities:
            metadata = state.lead_data.get('metadata', {})
            metadata.update(entities)
            state.lead_data['metadata'] = metadata
            logger.info(f"[CRMAgent] Entidades extraídas del link: {entities}")

        # Guardar en historial CRM
        if 'crm_history' not in state.lead_data:
            state.lead_data['crm_history'] = []
        state.lead_data['crm_history'].append(f"User: {message}")
        state.lead_data['crm_history'].append(f"Agent: {response_text}")

        logger.info(f"[CRMAgent] Respuesta generada para llegada por link de {nombre_portal}")

        return {
            "response": response_text,
            "new_state": state,
            "ready_for_handoff": False
        }

    def _extract_entities_from_link(self, url: str, message: str) -> Dict[str, Any]:
        """
        Intenta extraer información del inmueble basándose en el URL.

        Ejemplo: fincaraiz.com.co/apartamento-arriendo-poblado
        -> tipo_propiedad: apartamento, tipo_operacion: arriendo, ubicacion: poblado
        """
        entities = {}
        url_lower = url.lower() if url else ""

        # Detectar tipo de propiedad en URL
        tipos = ['apartamento', 'casa', 'local', 'oficina', 'bodega', 'lote']
        for tipo in tipos:
            if tipo in url_lower:
                entities['tipo_propiedad'] = tipo
                break

        # Detectar operación
        if 'arriendo' in url_lower or 'alquiler' in url_lower:
            entities['tipo_operacion'] = 'arriendo'
        elif 'venta' in url_lower:
            entities['tipo_operacion'] = 'venta'

        # Detectar ubicaciones conocidas del Área Metropolitana
        ubicaciones = [
            'poblado', 'laureles', 'envigado', 'sabaneta', 'itagui',
            'bello', 'medellin', 'rionegro', 'caldas', 'estrella'
        ]
        for ubi in ubicaciones:
            if ubi in url_lower:
                entities['ubicacion'] = ubi.title()
                break

        return entities

    def _extract_entities(self, message: str) -> Dict[str, Any]:
        """
        Extrae entidades inmobiliarias del mensaje del usuario usando LLM.
        Incluye manejo robusto de respuestas JSON malformadas.
        """
        try:
            extraction_prompt = PROPERTY_EXTRACTION_PROMPT.format(user_message=message)
            messages = [SystemMessage(content=extraction_prompt)]

            response = llama_client.invoke(messages)
            response_text = response.content.strip()

            logger.debug(f"[CRMAgent] Respuesta LLM extracción: {response_text[:200]}")

            # Parsear JSON de la respuesta - buscar bloque JSON completo
            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}')

            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                logger.debug("[CRMAgent] No se encontró JSON válido en respuesta")
                return {}

            json_str = response_text[start_idx:end_idx + 1]

            # Intentar parsear el JSON
            try:
                entities = json.loads(json_str)
            except json.JSONDecodeError:
                # Intentar limpiar JSON mal formateado (saltos de línea, etc.)
                json_str_clean = json_str.replace('\n', '').replace('\r', '')
                entities = json.loads(json_str_clean)

            # Filtrar valores vacíos o None
            entities = {k: v for k, v in entities.items() if v and str(v).strip()}

            if entities:
                logger.info(f"[CRMAgent] Entidades extraídas: {entities}")
            return entities

        except json.JSONDecodeError as e:
            logger.warning(f"[CRMAgent] JSON malformado: {e}")
            return {}
        except Exception as e:
            logger.warning(f"[CRMAgent] Error extrayendo entidades: {e}")
            return {}

    def _extract_name_from_message(self, message: str, state: ConversationState) -> str:
        """
        Intenta extraer un nombre completo del mensaje del usuario.
        IMPORTANTE: Solo intenta extracción si ya hubo al menos un turno de conversación CRM.
        Esto evita falsos positivos en el primer mensaje del usuario.
        """
        from utils.pii_validator import robust_extract_name

        crm_history = state.lead_data.get('crm_history', [])

        # GUARDIA: No intentar extraer nombre en el primer turno (sin historial CRM)
        # El usuario debe haber respondido al menos una vez al CRM Agent
        if len(crm_history) < 2:  # Necesitamos al menos 1 User + 1 Agent message previos
            logger.debug("[CRMAgent] Primer turno CRM - no se intenta extraer nombre aún")
            return None

        # Verificar si el último mensaje del agente pidió el nombre
        last_agent_msg = ""
        for entry in reversed(crm_history):
            if entry.startswith("Agent:"):
                last_agent_msg = entry[6:].strip().lower()
                break

        name_request_indicators = ['nombre', 'cómo te llamas', 'como te llamas', 'registrarte']
        agent_asked_name = any(ind in last_agent_msg for ind in name_request_indicators)

        # Solo intentar extracción de nombre si el agente lo pidió explícitamente
        if not agent_asked_name:
            return None

        # El agente pidió el nombre - intentar extracción
        # Primero con regex (patrones explícitos como "Me llamo X")
        name = robust_extract_name(message)
        if name:
            logger.info(f"[CRMAgent] Nombre extraído via regex: {name}")
            return name

        # Si el mensaje es corto (probable respuesta de nombre), usar LLM
        if len(message.split()) <= 6 and '?' not in message:
            immob_keywords = ['casa', 'apartamento', 'arriendo', 'compra', 'venta',
                              'local', 'oficina', 'barrio', 'zona', 'presupuesto',
                              'habitaciones', 'millones', 'no', 'si', 'sí', 'cita']
            message_lower = message.lower()
            if not any(kw in message_lower for kw in immob_keywords):
                try:
                    prompt = (
                        f"El siguiente mensaje es la respuesta de un usuario a la pregunta '¿Cuál es tu nombre completo?'. "
                        f"Extrae el nombre completo. Si el mensaje NO contiene un nombre de persona, responde 'NO_NAME'.\n"
                        f"Mensaje: \"{message}\"\n"
                        f"Responde SOLO con el nombre completo o 'NO_NAME'."
                    )
                    response = llama_client.invoke([HumanMessage(content=prompt)])
                    result = response.content.strip()
                    if result and result != "NO_NAME" and len(result) > 2 and len(result) < 60:
                        logger.info(f"[CRMAgent] Nombre extraído via LLM: {result}")
                        return result
                except Exception as e:
                    logger.warning(f"[CRMAgent] Error en extracción LLM de nombre: {e}")

        return None

    async def process_lead_handoff(self, user_input: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa la transferencia de lead a CRM (HubSpot).
        Se ejecuta cuando ya tenemos el nombre del cliente.
        """
        try:
            # 1. PREPARAR DATOS
            logger.info("[CRMAgent] Iniciando sincronización con HubSpot...")

            # Normalizar teléfono (session_id viene como "whatsapp:+...")
            normalized_phone = normalize_phone_e164(state.session_id)
            logger.info(f"[CRMAgent] Teléfono normalizado: {normalized_phone}")

            # Extraer datos del lead desde state
            lead_name = state.lead_data.get('name', 'Lead')
            name_parts = split_full_name(lead_name)

            # Formatear historial de conversación (general + CRM)
            full_history = state.history + state.lead_data.get('crm_history', [])
            conversation_text = format_conversation_history(full_history)

            # Preparar metadata de propiedad
            metadata = state.lead_data.get('metadata', {})

            # Calcular score de calidad del lead
            score_data = {
                "firstname": name_parts["firstname"],
                "lastname": name_parts["lastname"],
                "phone": normalized_phone,
                "metadata": metadata
            }
            lead_score = calculate_lead_score(score_data)

            # 2. MAPEO DE PROPIEDADES PARA HUBSPOT
            now_utc = datetime.now(timezone.utc)
            today_midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            timestamp_ms = str(int(today_midnight_utc.timestamp() * 1000))

            # Construir chatbot_preference (resumen rápido para el asesor)
            preference_parts = []
            if metadata.get("tipo_operacion"):
                preference_parts.append(f"Operación: {metadata['tipo_operacion']}")
            if metadata.get("tipo_propiedad"):
                preference_parts.append(f"Tipo: {metadata['tipo_propiedad']}")
            if metadata.get("caracteristicas"):
                preference_parts.append(f"Características: {metadata['caracteristicas']}")
            if metadata.get("tiempo"):
                preference_parts.append(f"Plazo: {metadata['tiempo']}")
            if metadata.get("comentarios_adicionales"):
                preference_parts.append(f"Notas: {metadata['comentarios_adicionales']}")

            chatbot_preference = " | ".join(preference_parts) if preference_parts else ""

            # PROPIEDADES DEL CONTACTO
            contact_properties = {
                "firstname": name_parts["firstname"],
                "lastname": name_parts["lastname"] or "WhatsApp",
                "phone": normalized_phone,
                "whatsapp_id": normalized_phone,
                "chatbot_property_type": metadata.get("tipo_propiedad", ""),
                "chatbot_rooms": str(metadata.get("caracteristicas", "")),
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": str(metadata.get("presupuesto", "")),
                "chatbot_urgency": metadata.get("tiempo", ""),
                "chatbot_preference": chatbot_preference,
                "chatbot_conversation": conversation_text,
                "chatbot_score": str(lead_score),
                "chatbot_timestamp": timestamp_ms
            }

            # Agregar email si fue proporcionado
            if metadata.get("correo"):
                contact_properties["email"] = metadata["correo"]
                contact_properties["chatbot_email"] = metadata["correo"]

            logger.info(f"[CRMAgent] Datos del contacto preparados. Score: {lead_score}/100")

            # ASIGNACIÓN AUTOMÁTICA
            channel_origin = self.assigner.detect_channel_origin(metadata, state.session_id)
            owner_id = self.assigner.get_next_owner(channel_origin)

            if owner_id:
                contact_properties["hubspot_owner_id"] = owner_id
                logger.info(f"[CRMAgent] Lead asignado a owner ID: {owner_id} (canal: {channel_origin})")
            else:
                logger.warning("[CRMAgent] No se pudo asignar owner. Lead será huérfano.")

            # 3. LÓGICA SEARCH-BEFORE-CREATE (Deduplicación)
            contact_id = await self.hubspot.search_contact_by_phone(normalized_phone)

            if contact_id:
                logger.info(f"[CRMAgent] Contacto existente encontrado: {contact_id}")
                await self.hubspot.update_contact(contact_id, contact_properties)
                action_type = "actualizado"
            else:
                logger.info("[CRMAgent] Creando nuevo contacto en HubSpot...")
                contact_id = await self.hubspot.create_contact(contact_properties)
                action_type = "registrado"

            logger.info(f"[CRMAgent] Contacto {action_type} exitosamente: {contact_id}")

            # 4. CREAR DEAL (Oportunidad de Venta)
            deal_name = f"Lead WhatsApp - {lead_name} - {metadata.get('tipo_propiedad', 'Propiedad')}"

            deal_properties = {
                "dealname": deal_name,
                "amount": self._parse_amount(metadata.get("presupuesto", "0")),
                "description": f"Lead capturado vía chatbot Sofía. Interesado en {metadata.get('ubicacion', 'propiedad')}.",
                "chatbot_property_type": metadata.get("tipo_propiedad", ""),
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": str(metadata.get("presupuesto", "")),
                "chatbot_score": str(lead_score),
                "chatbot_urgency": metadata.get("tiempo", ""),
            }

            # Asignar el mismo owner al Deal
            if owner_id:
                deal_properties["hubspot_owner_id"] = owner_id

            deal_id = await self.hubspot.create_deal(contact_id, deal_properties)
            logger.info(f"[CRMAgent] Deal creado exitosamente: {deal_id}")

            # ALERTAS PARA LEADS HUÉRFANOS
            if not owner_id:
                orphan_alert_system.log_orphan_lead(
                    contact_id=contact_id,
                    phone=normalized_phone,
                    reason="No hay owners activos disponibles para asignación",
                    metadata={
                        "lead_name": lead_name,
                        "channel_origin": channel_origin,
                        "deal_id": deal_id
                    }
                )

            # 5. RESPUESTA AL USUARIO
            state.status = ConversationStatus.TRANSFERRED_CRM
            response_text = CRM_CONFIRMATION_TEMPLATE.format(lead_name=name_parts["firstname"])

            return {
                "response": response_text,
                "new_state": state,
                "ready_for_handoff": True,
                "success": True
            }

        except Exception as e:
            logger.error(f"[CRMAgent] Error crítico en sincronización HubSpot: {e}", exc_info=True)

            error_response = (
                f"Gracias por tu interés, {state.lead_data.get('name', 'usuario')}. "
                "He recibido tu información pero tuve un inconveniente técnico al registrarla. "
                "Un asesor se comunicará contigo pronto de todas formas."
            )

            state.status = ConversationStatus.TRANSFERRED_CRM

            return {
                "response": error_response,
                "new_state": state,
                "ready_for_handoff": True,
                "success": False
            }

    def _parse_amount(self, budget_str: str) -> float:
        """
        Extrae un valor numérico de un string de presupuesto.
        """
        try:
            digits = ''.join(filter(str.isdigit, str(budget_str)))
            return float(digits) if digits else 0.0
        except Exception as e:
            logger.warning(f"[CRMAgent] No se pudo parsear presupuesto '{budget_str}': {e}")
            return 0.0


# Instancia global (Singleton)
crm_agent = CRMAgent()