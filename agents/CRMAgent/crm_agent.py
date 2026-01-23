# -*- coding: utf-8 -*-
# Agents/CRMAgent/crm_agent.py
"""
Agente de CRM con integración completa a HubSpot API v3.
Reemplaza el stub anterior con funcionalidad real de sincronización.
""" 
from datetime import datetime, timezone
from typing import Dict, Any
from state_manager import ConversationState
from integrations.hubspot.hubspot_client import HubSpotClient
from integrations.hubspot.hubspot_utils import (
    normalize_phone_e164,
    calculate_lead_score,
    split_full_name,
    format_conversation_history
)
from prompts.crm_prompts import CRM_CONFIRMATION_TEMPLATE
from integrations.hubspot.lead_assigner import lead_assigner, orphan_alert_system
from logging_config import logger


class CRMAgent:
    """
    Agente que gestiona leads de ventas sincronizando con HubSpot CRM.
    """

    def __init__(self):
        """Inicializa el agente con cliente HubSpot y asignador de leads."""
        self.hubspot = HubSpotClient()
        self.assigner = lead_assigner
        logger.info("[CRMAgent] Inicializado con integración HubSpot y LeadAssigner.")

    async def process_lead_handoff(self, user_input: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa la transferencia de lead a CRM (HubSpot).
        Incluye: propiedades chatbot_* en Contacto y Deal, chatbot_preference, chatbot_score en Deal.
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

            # Formatear historial de conversación
            conversation_text = format_conversation_history(state.history)

            # Preparar metadata de propiedad (capturada por ReceptionAgent)
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

            # ═══ CONSTRUIR chatbot_preference ═══
            preference_parts = []
            if metadata.get("tipo_operacion"):
                preference_parts.append(f"Operación: {metadata['tipo_operacion']}")
            if metadata.get("tipo_propiedad"):
                preference_parts.append(f"Tipo: {metadata['tipo_propiedad']}")
            if metadata.get("caracteristicas"):
                preference_parts.append(f"Características: {metadata['caracteristicas']}")
            if metadata.get("urgencia"):
                preference_parts.append(f"Urgencia: {metadata['urgencia']}")
            if metadata.get("comentarios_adicionales"):
                preference_parts.append(f"Notas: {metadata['comentarios_adicionales']}")

            chatbot_preference = " | ".join(preference_parts) if preference_parts else ""

            # ═══ PROPIEDADES DEL CONTACTO ═══
            contact_properties = {
                "firstname": name_parts["firstname"],
                "lastname": name_parts["lastname"] or "WhatsApp",
                "phone": normalized_phone,
                "whatsapp_id": normalized_phone,
                "chatbot_property_type": metadata.get("tipo_propiedad", ""),
                "chatbot_rooms": str(metadata.get("caracteristicas", "")),
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": str(metadata.get("presupuesto", "")),
                "chatbot_preference": chatbot_preference,
                "chatbot_conversation": conversation_text,
                "chatbot_score": str(lead_score),
                "chatbot_timestamp": timestamp_ms
            }

            logger.info(f"[CRMAgent] Datos del contacto preparados. Score: {lead_score}/100")

            # ═══ ASIGNACIÓN AUTOMÁTICA ═══
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

            # ═══ PROPIEDADES DEL DEAL (INCLUYENDO CHATBOT_*) ═══
            deal_properties = {
                "dealname": deal_name,
                "amount": self._parse_amount(metadata.get("presupuesto", "0")),
                "description": f"Lead capturado vía chatbot Sofía. Interesado en {metadata.get('ubicacion', 'propiedad')}.",
                # Propiedades custom del chatbot
                "chatbot_property_type": metadata.get("tipo_propiedad", ""),
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": str(metadata.get("presupuesto", "")),
                "chatbot_score": str(lead_score),
            }

            # Asignar el mismo owner al Deal
            if owner_id:
                deal_properties["hubspot_owner_id"] = owner_id

            deal_id = await self.hubspot.create_deal(contact_id, deal_properties)
            logger.info(f"[CRMAgent] Deal creado exitosamente: {deal_id}")

            # ═══ ALERTAS PARA LEADS HUÉRFANOS ═══
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
            response_text = CRM_CONFIRMATION_TEMPLATE.format(lead_name=name_parts["firstname"])

            return {
                "response": response_text,
                "new_state": state,
                "success": True
            }

        except Exception as e:
            logger.error(f"[CRMAgent] Error crítico en sincronización HubSpot: {e}", exc_info=True)

            error_response = (
                f"Gracias por tu interés, {state.lead_data.get('name', 'usuario')}. "
                "He recibido tu información pero tuve un inconveniente técnico al registrarla. "
                "Un asesor se comunicará contigo pronto de todas formas."
            )

            return {
                "response": error_response,
                "new_state": state,
                "success": False
            }

    def _parse_amount(self, budget_str: str) -> float:
        """
        Extrae un valor numérico de un string de presupuesto.
        """
        try:
            # Extraer solo dígitos
            digits = ''.join(filter(str.isdigit, str(budget_str)))
            return float(digits) if digits else 0.0
        except Exception as e:
            logger.warning(f"[CRMAgent] No se pudo parsear presupuesto '{budget_str}': {e}")
            return 0.0


# Instancia global (Singleton)
crm_agent = CRMAgent()