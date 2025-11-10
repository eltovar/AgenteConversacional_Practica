# agent.py (OPTIMIZADO Y CORREGIDO)
from llm_client import llama_client
# 1. Importar el servicio RAG (lo usaremos directamente)
from rag import rag_service
# 2. Importar la lista de tools correcta (ALL_TOOLS) desde tool.py
from info_tool import ALL_TOOLS, informacion_empresa_func 
from prompts.info_prompts import SYSTEM_AGENT_PROMPT, TOOL_DECISION_PROMPT
from langchain_core.messages import SystemMessage, HumanMessage
import json
import re

# Nota: Asumo que la Tool de RAG se llama 'search_rag_knowledge' en tool.py
RAG_TOOL_NAME = "search_rag_knowledge" 
INFO_EMPRESA_TOOL_NAME = "info_empresa_contacto_filosofia"

class infoAgent:
    def __init__(self, tools=ALL_TOOLS): # Usamos ALL_AGENT_TOOLS de tool.py
        self.tools = {tool.name: tool for tool in tools}
        self.tool_descriptions = "\n".join([f"- {t.name}: {t.description}" for t in tools])

    def _run_tool(self, tool_name: str, tool_input: dict) -> str:
        """Ejecuta una herramienta y devuelve la respuesta."""
        
        # Corrección: El RAG se maneja con la función que usa rag.py
        if tool_name == "info_empresa_contacto_filosofia":
            # Usar rag_service para buscar en la base de conocimiento
            query = tool_input.get('query', tool_input.get('tema', 'información de la empresa'))
            context = rag_service.search_knowledge(query)
            return f"Contexto recuperado: {context}"

        # Manejo de la tool de info_empresa (si esta tool solo sirve como un alias para RAG)
        if tool_name == INFO_EMPRESA_TOOL_NAME:
            # Si esta tool es la que decide la consulta, redirigimos a RAG
            query = tool_input.get('tema', 'información de la empresa')
            context = rag_service.search_knowledge(query)
            return context


        return f"Tool '{tool_name}' no encontrada o no implementada."

    def _determine_tool_call(self, user_input: str) -> dict | None:
        """
        Determina si la consulta del usuario requiere una Tool.
        """
        # ... (El resto de la lógica _determine_tool_call queda igual)
        full_instruction_prompt = (
            TOOL_DECISION_PROMPT.format(user_input=user_input) +
            f"\n\nHerramientas disponibles:\n{self.tool_descriptions}" +
            "\n\nResponde ÚNICAMENTE con un JSON válido en el formato: "
            '{"tool_name": "nombre_tool", "tool_input": {"param1": "valor1", "param2": "valor2"}}. '
            "Si no se requiere ninguna herramienta, responde ÚNICAMENTE: NO_TOOL"
        )
        
        messages = [
            SystemMessage(content=SYSTEM_AGENT_PROMPT),
            HumanMessage(content=full_instruction_prompt)
        ]

        response = llama_client.invoke(messages).content

        response_clean = response.strip()

        if "NO_TOOL" in response_clean.upper():
            return None

        try:
            json_match = re.search(r'\{.*\}', response_clean, re.DOTALL)
            if json_match:
                tool_call = json.loads(json_match.group(0))
                if 'tool_name' in tool_call and 'tool_input' in tool_call:
                    return tool_call

                print(f"⚠️ Warning: JSON encontrado pero estructura incorrecta: {tool_call}")
                return None
            else:
                return None
        except json.JSONDecodeError:
            print(f"⚠️ Error al parsear JSON de Tool. Respuesta LLM: {response_clean[:50]}...")
            return None


    def process_info_query(self, user_input: str) -> str:
        """Procesa la consulta del usuario, usando el flujo de decisión."""
        print(f"\nUsuario: {user_input}")

        # 1. Detección de Tools
        tool_call = self._determine_tool_call(user_input)

        if tool_call and 'tool_name' in tool_call and 'tool_input' in tool_call:
            tool_name = tool_call['tool_name']
            tool_input = tool_call['tool_input']

            print(f"🤖 Agente (Tool): Ejecutando Tool '{tool_name}' con input: {tool_input}")

            # 🚨 CORRECCIÓN CLAVE AQUÍ 🚨
            if tool_name == INFO_EMPRESA_TOOL_NAME:
                print("🤖 Agente (RAG): Pasando a recuperar contexto de RAG...")
                
                # 1. Ejecución de la Tool (que ahora contiene el RAG)
                # La función _run_tool ya resuelve la llamada a rag_service.search_knowledge
                context = self._run_tool(tool_name, tool_input) 
                
                # 2. Generación de la Respuesta Final con Contexto
                rag_prompt = (
                    f"{SYSTEM_AGENT_PROMPT}\n\n"
                    f"Tu herramienta te ha devuelto información. "
                    f"Usa el siguiente contexto para responder a la pregunta original del usuario: '{user_input}'.\n"
                    f"Contexto: {context}"
                )

                messages = [
                    SystemMessage(content=rag_prompt),
                    HumanMessage(content=user_input)
                ]

                response = llama_client.invoke(messages).content
                return f"💬 Agente (RAG, impulsado por Tool): {response}"

            # Si fuera otra Tool (hypotética), ejecutaría aquí
            tool_response = self._run_tool(tool_name, tool_input)
            return f"✅ Respuesta de la Tool ({tool_name}): {tool_response}"


        # 2. LLM Base (Respuesta conversacional general)
        print("🤖 Agente (LLM): Respondiendo directamente...")
        messages = [
            SystemMessage(content=SYSTEM_AGENT_PROMPT),
            HumanMessage(content=user_input)
        ]
        response = llama_client.invoke(messages).content
        return f"💡 Agente (LLM): {response}"

# Instancia global
agent = infoAgent()