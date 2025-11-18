# -*- coding: utf-8 -*-
# leadsales_agent.py

from state_manager import ConversationState
from typing import Dict, Any
from logging_config import logger
from prompts.leadsales_prompts import LEADSALES_CONFIRMATION_TEMPLATE

class LeadSalesAgent:
    """
    Agente que simula la gestion de un lead de ventas.
    Actualmente es un stub que solo registra el lead capturado.
    """

    def __init__(self):
        # En una version futura, podria cargar prompts o herramientas especificas.
        logger.info("[LeadSalesAgent] Inicializado.")

    def process_lead_handoff(self, user_input: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa un mensaje del usuario tras la transferencia de lead.

        Dado que ReceptionAgent ya capturo el nombre y transfirio el estado a
        TRANSFERRED_LEADSALES, este agente solo debe confirmar la recepcion y
        registrar los datos capturados.
        """

        # 1. Recuperar datos del lead (nombre ya capturado por ReceptionAgent)
        lead_name = state.lead_data.get('name', 'cliente')

        # 2. Registrar/Simular Transferencia
        # NOTA: En este punto, el lead ya fue registrado por ReceptionAgent
        # (ver reception_agent.py, linea 145 donde se simula transferencia a CRM).

        logger.info(f"[LEADSALES] Procesando mensaje en estado TRANSFERRED_LEADSALES. Lead: {lead_name}")

        # 3. Respuesta al usuario usando template con personalidad de Sofía
        response_text = LEADSALES_CONFIRMATION_TEMPLATE.format(lead_name=lead_name)

        # 4. Transicion
        # El reset de estado (TRANSFERRED_LEADSALES -> RECEPTION_START) se maneja en main.py.
        # Por convencion, este agente NO cambia el estado, sino que permite que main.py lo fuerce.

        return {
            "response": response_text,
            "new_state": state
        }

# Instancia global (Singleton)
lead_sales_agent = LeadSalesAgent()
