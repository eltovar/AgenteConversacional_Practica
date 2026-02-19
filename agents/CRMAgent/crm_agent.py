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
from integrations.hubspot.pipeline_router import (
    get_target_pipeline,
    get_analytics_source,
)
from prompts.crm_prompts import (
    CRM_SYSTEM_PROMPT,
    CRM_CONFIRMATION_TEMPLATE,
    PROPERTY_EXTRACTION_PROMPT,
    LINK_ARRIVAL_CONTEXT,
    NAME_EXTRACTION_PROMPT,
    FIRST_MESSAGE_CONTEXT
)
from integrations.hubspot.lead_assigner import lead_assigner, orphan_alert_system, LeadAssigner
from llm_client import llama_client

# Importación para activar HUMAN_ACTIVE en el panel
import os
import redis.asyncio as aioredis
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
            # IMPORTANTE: Solo extraer si NO tenemos nombre ya registrado
            existing_name = state.lead_data.get('name')
            if existing_name and str(existing_name).strip():
                # Ya tenemos nombre, NO intentar extraer otro
                logger.debug(f"[CRMAgent] Nombre ya existe: {existing_name}, omitiendo extracción")
            else:
                # No tenemos nombre, intentar extraer
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
        # Detectar si es primer mensaje para incluir presentación
        # NOTA: Si llegó por link, _handle_link_arrival ya incluyó la presentación
        is_first_message = state.metadata.get("is_first_message", False)
        llegada_por_link = state.metadata.get("llegada_por_link", False)
        crm_history = state.lead_data.get('crm_history', [])

        # Solo agregar presentación si:
        # - Es primer mensaje
        # - NO llegó por link (link arrival ya maneja presentación)
        # - No hay historial previo (ya se presentó antes)
        should_include_intro = is_first_message and not llegada_por_link and len(crm_history) == 0

        if should_include_intro:
            logger.info("[CRMAgent] Primer mensaje detectado - incluirá presentación")
            state.metadata["is_first_message"] = False  # Limpiar flag

        # Construir contexto con datos ya recopilados
        metadata = state.lead_data.get('metadata', {})
        context_info = self._build_context_summary(metadata)

        system_content = CRM_SYSTEM_PROMPT

        # Añadir instrucciones de presentación SOLO si no llegó por link y no hay historial
        if should_include_intro:
            system_content += "\n\n" + FIRST_MESSAGE_CONTEXT

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
        # Limpiar flag de primer mensaje (LINK_ARRIVAL_CONTEXT ya incluye presentación)
        if state.metadata.get("is_first_message"):
            state.metadata["is_first_message"] = False

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
            "whatsapp_directo": "WhatsApp",
            "desconocido": "internet",
        }
        nombre_portal_amigable = portal_nombres.get(nombre_portal, "internet")

        # PRIMERO: Extraer entidades del link/mensaje ANTES de generar respuesta
        entities = self._extract_entities_from_link(url, message)
        if entities:
            metadata = state.lead_data.get('metadata', {})
            metadata.update(entities)
            state.lead_data['metadata'] = metadata
            logger.info(f"[CRMAgent] Entidades extraídas del link: {entities}")

        # Construir descripción del inmueble para el prompt
        info_inmueble = self._build_property_description(entities)

        # Construir prompt con contexto del link E información del inmueble
        link_context = LINK_ARRIVAL_CONTEXT.format(
            nombre_portal=nombre_portal_amigable,
            url_referencia=url,
            info_inmueble=info_inmueble
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

        # Guardar en historial CRM
        if 'crm_history' not in state.lead_data:
            state.lead_data['crm_history'] = []
        state.lead_data['crm_history'].append(f"User: {message}")
        state.lead_data['crm_history'].append(f"Agent: {response_text}")

        logger.info(f"[CRMAgent] Respuesta generada para llegada por link de {nombre_portal}")

        # Marcar como procesado para no volver a entrar en este flujo
        state.metadata["link_procesado"] = True

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

    def _build_property_description(self, entities: Dict[str, Any]) -> str:
        """
        Construye una descripción legible del inmueble basada en las entidades extraídas.
        Esta descripción se incluye en el prompt para que Sofía sepa qué inmueble le interesa al cliente.
        """
        if not entities:
            return "No se pudo extraer información específica del inmueble del link."

        parts = []

        tipo = entities.get('tipo_propiedad')
        operacion = entities.get('tipo_operacion')
        ubicacion = entities.get('ubicacion')

        if tipo and operacion and ubicacion:
            # Caso completo: "Casa en arriendo en Envigado"
            parts.append(f"- Tipo: {tipo.capitalize()} en {operacion}")
            parts.append(f"- Ubicación: {ubicacion}")
        elif tipo and operacion:
            parts.append(f"- Tipo: {tipo.capitalize()} en {operacion}")
        elif tipo and ubicacion:
            parts.append(f"- Tipo: {tipo.capitalize()}")
            parts.append(f"- Ubicación: {ubicacion}")
        elif tipo:
            parts.append(f"- Tipo: {tipo.capitalize()}")
        elif ubicacion:
            parts.append(f"- Ubicación: {ubicacion}")

        if not parts:
            return "No se pudo extraer información específica del inmueble del link."

        return "\n".join(parts)

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
        Extrae nombre de persona del mensaje usando LLM.
        VERSIÓN AGRESIVA: Siempre intenta extraer, sin importar longitud del mensaje
        ni si el agente preguntó explícitamente por el nombre.

        El nombre es el "gatillo" para enviar a HubSpot, así que es crítico detectarlo.
        """
        from utils.pii_validator import robust_extract_name

        crm_history = state.lead_data.get('crm_history', [])

        # GUARDIA MÍNIMA: Solo evitar extracción en el primer turno absoluto
        # (para no confundir saludos con nombres)
        if len(crm_history) < 2:
            logger.debug("[CRMAgent] Primer turno CRM - no se intenta extraer nombre aún")
            return None

        # 1. INTENTO RÁPIDO: Regex para patrones explícitos ("Me llamo X", "Soy X")
        name = robust_extract_name(message)
        if name:
            logger.info(f"[CRMAgent] Nombre extraído via regex: {name}")
            return name

        # 2. INTENTO PRINCIPAL: Usar LLM para extraer nombre de CUALQUIER mensaje
        # Sin límite de palabras - el LLM es capaz de encontrar nombres en contexto
        try:
            # Construir contexto de la conversación para ayudar al LLM
            conversation_context = ""
            for entry in crm_history[-6:]:  # Últimos 3 turnos (6 mensajes)
                conversation_context += f"{entry}\n"

            prompt = NAME_EXTRACTION_PROMPT.format(
                conversation_context=conversation_context,
                message=message
            )

            response = llama_client.invoke([HumanMessage(content=prompt)])
            result = response.content.strip()

            # Validar respuesta del LLM
            if result and result != "NO_NAME":
                # Limpiar posibles artefactos de la respuesta
                result = result.replace('"', '').replace("'", "").strip()

                # Validaciones básicas de que es un nombre válido
                if (len(result) >= 2 and
                    len(result) <= 60 and
                    not any(word in result.lower() for word in ['no_name', 'no hay', 'no encuentro', 'ninguno'])):

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

            # ASIGNACIÓN DE CANAL (mover antes de calcular score para evitar UnboundLocalError)
            # Primero verificar si el canal viene de state.metadata (llegada por link)
            # Si no, usar detect_channel_origin para detectarlo de lead_data.metadata
            if state.metadata.get("canal_origen"):
                channel_origin = state.metadata["canal_origen"]
            else:
                channel_origin = self.assigner.detect_channel_origin(metadata, state.session_id)

            # Calcular score de calidad del lead (incluye bonus por canal y código)
            score_data = {
                "firstname": name_parts["firstname"],
                "lastname": name_parts["lastname"],
                "phone": normalized_phone,
                "metadata": metadata,
                "canal_origen": channel_origin,
                "property_code": state.metadata.get("property_code"),
                "llegada_por_link": state.metadata.get("llegada_por_link", False),
                "es_inmueble": state.metadata.get("es_inmueble", False),
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

            # Obtener pipeline y stage basado en canal de origen (channel_origin ya fue asignado arriba)
            pipeline_config = get_target_pipeline(channel_origin)
            analytics_source = get_analytics_source(channel_origin)

            owner_id = self.assigner.get_next_owner(channel_origin)
            logger.info(f"[CRMAgent] Canal: {channel_origin}, Pipeline: {'Redes Sociales' if pipeline_config.get('is_social_media') else 'General'}")

            # PROPIEDADES DEL CONTACTO
            # Normalizar presupuesto a número para HubSpot (chatbot_budget es INTEGER)
            budget_raw = metadata.get("presupuesto", "")
            budget_numeric = self._parse_budget_to_number(budget_raw)

            # Formatear características como texto multilínea con viñetas
            features_formatted = self._format_features_as_text(metadata.get("caracteristicas"))

            contact_properties = {
                "firstname": name_parts["firstname"],
                "lastname": name_parts["lastname"] or "WhatsApp",
                "phone": normalized_phone,
                "whatsapp_id": normalized_phone,
                "chatbot_property_type": metadata.get("tipo_propiedad", ""),
                "chatbot_rooms": features_formatted,  # Ahora es texto multilínea con viñetas
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": budget_numeric,  # Número entero para HubSpot
                "chatbot_urgency": metadata.get("tiempo", ""),
                "chatbot_operation_type": metadata.get("tipo_operacion", ""),  # NUEVO: Tipo de operación
                "chatbot_preference": chatbot_preference,
                "chatbot_conversation": conversation_text,
                "chatbot_score": str(lead_score),
                "chatbot_timestamp": timestamp_ms,
                "canal_origen": channel_origin,  # Canal de origen para workflows y reportes
                "hs_analytics_source": analytics_source,  # Categoría macro para gráficos de HubSpot
            }

            # Agregar email si fue proporcionado
            if metadata.get("correo"):
                contact_properties["email"] = metadata["correo"]
                contact_properties["chatbot_email"] = metadata["correo"]

            # Asignar owner si está disponible
            if owner_id:
                contact_properties["hubspot_owner_id"] = owner_id
                logger.info(f"[CRMAgent] Lead asignado a owner ID: {owner_id} (canal: {channel_origin})")
            else:
                logger.warning("[CRMAgent] No se pudo asignar owner. Lead será huérfano.")

            logger.info(f"[CRMAgent] Datos del contacto preparados. Score: {lead_score}/100")

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
                "chatbot_operation_type": metadata.get("tipo_operacion", ""),  # NUEVO: Tipo de operación
                "chatbot_location": metadata.get("ubicacion", ""),
                "chatbot_budget": budget_numeric,  # Reutilizar el valor numérico calculado
                "chatbot_rooms": features_formatted,  # Características formateadas
                "chatbot_score": str(lead_score),
                "chatbot_urgency": metadata.get("tiempo", ""),
                "chatbot_conversation": conversation_text,  # Historial completo de la conversación
                "canal_origen": channel_origin,  # Canal de origen del lead
            }

            # Asignar el mismo owner al Deal
            if owner_id:
                deal_properties["hubspot_owner_id"] = owner_id

            # Crear deal con el pipeline correspondiente al canal de origen
            deal_id = await self.hubspot.create_deal(
                contact_id,
                deal_properties,
                pipeline_id=pipeline_config.get("pipeline_id"),
                dealstage=pipeline_config.get("stage_id")
            )
            logger.info(f"[CRMAgent] Deal creado exitosamente: {deal_id}")

            # ═══════════════════════════════════════════════════════════════
            # ACTIVAR HUMAN_ACTIVE PARA QUE APAREZCA EN EL PANEL
            # ═══════════════════════════════════════════════════════════════
            # Esto hace que el contacto aparezca automáticamente en el panel
            # de asesores con badge "En espera", como en WhatsApp Web
            try:
                await self._activate_human_in_panel(
                    phone_normalized=normalized_phone,
                    contact_id=contact_id,
                    owner_id=owner_id,
                    reason=f"Lead registrado via CRM - {metadata.get('tipo_operacion', 'consulta')}",
                    display_name=lead_name,
                    canal_origen=channel_origin
                )
                logger.info(f"[CRMAgent] ✅ HUMAN_ACTIVE activado - Contacto aparecerá en panel")
            except Exception as e:
                logger.warning(f"[CRMAgent] No se pudo activar HUMAN_ACTIVE: {e}")

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

    def _format_features_as_text(self, features) -> str:
        """
        Convierte las características a formato texto multilínea para HubSpot.

        Input puede ser:
        - Lista: ["3 habitaciones", "2 baños", "parqueadero"]
        - String: "3 habitaciones, 2 baños"
        - None/vacío

        Output:
        "• 3 habitaciones
        • 2 baños
        • parqueadero"
        """
        if not features:
            return ""

        # Si ya es una lista, formatear directamente
        if isinstance(features, list):
            if not features:
                return ""
            return "\n".join(f"• {item.strip()}" for item in features if item and str(item).strip())

        # Si es string, intentar detectar si tiene múltiples características
        text = str(features).strip()
        if not text:
            return ""

        # Detectar si es una lista separada por comas o "y"
        # Ej: "3 habitaciones, 2 baños y parqueadero"
        if ',' in text or ' y ' in text.lower():
            # Reemplazar " y " por coma para unificar
            text = text.lower().replace(' y ', ', ')
            items = [item.strip() for item in text.split(',') if item.strip()]
            if len(items) > 1:
                return "\n".join(f"• {item}" for item in items)

        # Si es un solo valor, retornarlo con viñeta
        return f"• {text}"

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

    def _parse_budget_to_number(self, budget_str: str) -> int:
        """
        Convierte un string de presupuesto a número entero para HubSpot.
        Maneja formatos comunes en español:
        - "200 millones" → 200000000
        - "200millones" → 200000000
        - "2.5 millones" → 2500000
        - "500 mil" → 500000
        - "200.000.000" → 200000000
        - "200,000,000" → 200000000
        - "$200.000.000" → 200000000
        """
        import re

        if not budget_str:
            return 0

        try:
            text = str(budget_str).lower().strip()

            # Remover símbolos de moneda
            text = text.replace('$', '').replace('cop', '').strip()

            # Caso: "X millones" o "X millones de pesos"
            millon_match = re.search(r'(\d+(?:[.,]\d+)?)\s*mill[oó]n(?:es)?', text)
            if millon_match:
                num_str = millon_match.group(1).replace(',', '.')
                return int(float(num_str) * 1_000_000)

            # Caso: "X mil" (ej: "500 mil")
            mil_match = re.search(r'(\d+(?:[.,]\d+)?)\s*mil\b', text)
            if mil_match:
                num_str = mil_match.group(1).replace(',', '.')
                return int(float(num_str) * 1_000)

            # Caso: número con separadores (200.000.000 o 200,000,000)
            # Primero intentar formato colombiano (punto como separador de miles)
            cleaned = re.sub(r'[^\d.,]', '', text)

            if cleaned:
                # Si tiene puntos como separadores de miles (formato colombiano)
                if '.' in cleaned and ',' not in cleaned:
                    # 200.000.000 → 200000000
                    cleaned = cleaned.replace('.', '')
                elif ',' in cleaned and '.' not in cleaned:
                    # 200,000,000 → 200000000
                    cleaned = cleaned.replace(',', '')
                elif ',' in cleaned and '.' in cleaned:
                    # Formato mixto - asumir punto es miles, coma es decimal
                    # 2.500,00 → 2500.00
                    cleaned = cleaned.replace('.', '').replace(',', '.')

                return int(float(cleaned)) if cleaned else 0

            return 0

        except Exception as e:
            logger.warning(f"[CRMAgent] No se pudo parsear presupuesto a número '{budget_str}': {e}")
            return 0


    async def _activate_human_in_panel(
        self,
        phone_normalized: str,
        contact_id: str,
        owner_id: str = None,
        reason: str = None,
        display_name: str = None,
        canal_origen: str = None
    ) -> None:
        """
        Activa HUMAN_ACTIVE en Redis para que el contacto aparezca
        automáticamente en el panel de asesores.

        Este método replica la lógica de ConversationStateManager.activate_human()
        pero se ejecuta directamente desde CRMAgent para evitar dependencias circulares.
        """
        from datetime import datetime, timedelta
        import json

        # Configuración
        STATE_PREFIX = "conv_state:"
        META_PREFIX = "conv_meta:"
        HANDOFF_TTL_SECONDS = 2 * 60 * 60  # 2 horas

        # Obtener URL de Redis
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))

        try:
            # Conectar a Redis
            r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)

            # 1. Guardar estado HUMAN_ACTIVE con TTL de 2 horas
            state_key = f"{STATE_PREFIX}{phone_normalized}"
            await r.set(state_key, "HUMAN_ACTIVE", ex=HANDOFF_TTL_SECONDS)

            # 2. Guardar metadata para el panel
            now = datetime.now()
            expires_at = now + timedelta(seconds=HANDOFF_TTL_SECONDS)

            meta = {
                "phone_normalized": phone_normalized,
                "contact_id": contact_id,
                "status": "HUMAN_ACTIVE",
                "last_activity": now.isoformat(),
                "handoff_reason": reason,
                "assigned_owner_id": owner_id,
                "canal_origen": canal_origen,
                "display_name": display_name,
                "message_count": 0,
                "created_at": now.isoformat()
            }

            meta_key = f"{META_PREFIX}{phone_normalized}"
            await r.set(meta_key, json.dumps(meta), ex=HANDOFF_TTL_SECONDS)

            await r.close()

            logger.info(
                f"[CRMAgent] HUMAN_ACTIVE activado: {phone_normalized} "
                f"(owner: {owner_id or 'sin asignar'}, "
                f"expira: {expires_at.strftime('%H:%M:%S')})"
            )

        except Exception as e:
            logger.error(f"[CRMAgent] Error activando HUMAN_ACTIVE en Redis: {e}")
            raise


# Instancia global (Singleton)
crm_agent = CRMAgent()