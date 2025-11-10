# llm_client.py
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from dotenv import load_dotenv
from typing import List
import os

load_dotenv()

class LLMClient:
    def __init__(self, model_name: str = "gpt-4o-mini", temperature: float = 0.1):
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY no encontrada en .env")

        self.client = ChatOpenAI(
            model=model_name,
            temperature=temperature,
            # API key se lee automáticamente del entorno
        )

    # El wrapper de LangChain maneja la invocación de tools nativamente
    def invoke(self, messages, tools=None, tool_choice=None):
        kwargs = {}
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        return self.client.invoke(messages, **kwargs)

# Instancia global
llama_client = LLMClient()