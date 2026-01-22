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
from logging_config import logger


class CRMAgent:
    """
    Agente que gestiona leads de ventas sincronizando con HubSpot CRM.
    """

    def __init__(self):
        """Inicializa el agente con cliente HubSpot."""
        self.hubspot = HubSpotClient()
        logger.info("[CRMAgent] Inicializado con integración HubSpot.")

    async def process_lead_handoff(self, user_input: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa la transferencia de lead a CRM (HubSpot).
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
            # NOTA: HubSpot Date Picker requiere Unix timestamp en milisegundos a MEDIANOCHE UTC
            # Propiedades numéricas deben enviarse como strings
            now_utc = datetime.now(timezone.utc)
            today_midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            timestamp_ms = str(int(today_midnight_utc.timestamp() * 1000))

            contact_properties = {
                "firstname": name_parts["firstname"],
                "lastname": name_parts["lastname"] or "WhatsApp",  # Fallback si no hay apellido
                "phone": normalized_phone,
                "whatsapp_id": normalized_phone,  # ID único para deduplicación
                "chatbot_property_type": metadata.get("tipo_propiedad", ""),
                "chatbot_rooms": str(metadata.get("caracteristicas", "")),  # Habitaciones, área, etc.
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": str(metadata.get("presupuesto", "")),
                "chatbot_conversation": conversation_text,
                "chatbot_score": str(lead_score),  # HubSpot Number como string
                "chatbot_timestamp": timestamp_ms  # Unix timestamp en milisegundos
            }

            logger.info(f"[CRMAgent] Datos del contacto preparados. Score: {lead_score}/100")

            # 3. LÓGICA SEARCH-BEFORE-CREATE (Deduplicación)
            contact_id = await self.hubspot.search_contact_by_phone(normalized_phone)

            if contact_id:
                # Contacto existente → ACTUALIZAR
                logger.info(f"[CRMAgent] Contacto existente encontrado: {contact_id}")
                await self.hubspot.update_contact(contact_id, contact_properties)
                action_type = "actualizado"
            else:
                # Contacto nuevo → CREAR
                logger.info("[CRMAgent] Creando nuevo contacto en HubSpot...")
                contact_id = await self.hubspot.create_contact(contact_properties)
                action_type = "registrado"

            logger.info(f"[CRMAgent] Contacto {action_type} exitosamente: {contact_id}")

            # 4. CREAR DEAL (Oportunidad de Venta)
            deal_name = f"Lead WhatsApp - {lead_name} - {metadata.get('tipo_propiedad', 'Propiedad')}"
            deal_properties = {
                "dealname": deal_name,
                "amount": self._parse_amount(metadata.get("presupuesto", "0")),
                "description": f"Lead capturado vía chatbot. Interesado en {metadata.get('ubicacion', 'propiedad')}."
            }

            deal_id = await self.hubspot.create_deal(contact_id, deal_properties)
            logger.info(f"[CRMAgent] Deal creado exitosamente: {deal_id}")

            # 5. RESPUESTA AL USUARIO
            response_text = CRM_CONFIRMATION_TEMPLATE.format(lead_name=name_parts["firstname"])

            return {
                "response": response_text,
                "new_state": state,
                "success": True
            }

        except Exception as e:
            # Manejo de errores: No exponer detalles técnicos al usuario
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