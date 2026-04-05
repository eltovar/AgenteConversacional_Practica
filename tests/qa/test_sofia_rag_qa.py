# tests/qa/test_sofia_rag_qa.py
# -*- coding: utf-8 -*-
"""
QA SUITE: Simulación de Cliente - Validación de Prompts y RAGs de Sofía
═══════════════════════════════════════════════════════════════════════════
Objetivo: Verificar que Sofía responde correctamente a preguntas de clientes
reales, usando los datos correctos de la Knowledge Base.

Estrategia:
1. Cargar contenido REAL de los archivos KB (no texto inventado)
2. Simular decisión del LLM (tool call o respuesta directa)
3. Verificar que los documentos correctos son consultados
4. Verificar que el contexto enviado al LLM contiene info crítica
5. Detectar anomalías: números incorrectos, URLs faltantes, etc.

Cobertura por KB Document:
  ✅ informacion_institucional.txt     → historia, misión, horarios, contacto
  ✅ info_cobertura_propiedades.txt    → municipios, tipos de propiedad
  ✅ info_pagos_online.txt             → métodos de pago, URL pagos, número
  ✅ soporte_caja_pagos.txt            → caja, certificados, consignaciones
  ✅ soporte_contabilidad_facturas.txt → facturas, certificados tributarios
  ✅ soporte_contratos_terminacion.txt → terminación, prórroga, convivencia
  ✅ soporte_departamentos.txt         → admin, jurídico, servicios públicos, reparaciones
  ✅ info_estudios_libertador.txt      → bypass RAG, respuesta fija

Anomalías detectadas:
  ❌ Número correcto '604 444 63 64' en todos los archivos
  ❌ URL de pagos presente: https://inmobiliariaproteger.com/main-servicio-id-11952-q-pagos_online.htm?page=1
  ❌ Sin números obsoletos ('322 502 1493', etc.)
"""

import os
import re
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
from typing import Dict, List, Optional, Tuple
from langchain_core.messages import AIMessage

# ── Variables de entorno mínimas para importar sin errores ──────────────────
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-for-qa")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("HUBSPOT_API_KEY", "test-key")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+15005550006")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES DE QA — Valores críticos que DEBEN aparecer en las respuestas
# ═══════════════════════════════════════════════════════════════════════════════

NUMERO_CORRECTO = "604 444 63 64"
NUMERO_INCORRECTO = "604 444 6364"
URL_PAGOS = "https://inmobiliariaproteger.com/main-servicio-id-11952-q-pagos_online.htm?page=1"
NUMEROS_OBSOLETOS = ["322 502 1493", "3225021493", "+573225021493"]
KB_DIR = Path("knowledge_base")


# ═══════════════════════════════════════════════════════════════════════════════
# CARGA DE CONTENIDO REAL DE KB (fuente de verdad para los tests)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_kb_file(filename: str) -> str:
    """Carga el contenido real de un archivo KB. Falla si no existe."""
    path = KB_DIR / filename
    assert path.exists(), f"[QA-CRÍTICO] Archivo KB no encontrado: {path}"
    return path.read_text(encoding="utf-8")


# Contenido real de cada KB — cargado una sola vez al importar el módulo
KB_CONTENT = {
    "info_institucional":           _load_kb_file("informacion_institucional.txt"),
    "info_cobertura":               _load_kb_file("info_cobertura_propiedades.txt"),
    "info_pagos":                   _load_kb_file("info_pagos_online.txt"),
    "soporte_caja":                 _load_kb_file("soporte_caja_pagos.txt"),
    "soporte_contabilidad":         _load_kb_file("soporte_contabilidad_facturas.txt"),
    "soporte_contratos":            _load_kb_file("soporte_contratos_terminacion.txt"),
    "soporte_departamentos":        _load_kb_file("soporte_departamentos.txt"),
    "info_libertador":              _load_kb_file("info_estudios_libertador.txt"),
}

# Mapping: ruta doc → clave en KB_CONTENT
DOC_PATH_TO_KEY = {
    "knowledge_base/informacion_institucional.txt":     "info_institucional",
    "knowledge_base/info_cobertura_propiedades.txt":    "info_cobertura",
    "knowledge_base/info_pagos_online.txt":             "info_pagos",
    "knowledge_base/soporte_departamentos.txt":         "soporte_departamentos",
    "knowledge_base/soporte_caja_pagos.txt":            "soporte_caja",
    "knowledge_base/soporte_contabilidad_facturas.txt": "soporte_contabilidad",
    "knowledge_base/soporte_contratos_terminacion.txt": "soporte_contratos",
    "knowledge_base/info_estudios_libertador.txt":      "info_libertador",
}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS DE TEST
# ═══════════════════════════════════════════════════════════════════════════════

def _make_tool_response(tool_name: str, tema: str = "consulta") -> AIMessage:
    """Crea un AIMessage que simula al LLM eligiendo una tool."""
    return AIMessage(
        content="",
        tool_calls=[{
            "name": tool_name,
            "args": {"tema": tema},
            "id": f"call_{tool_name}_{tema[:8]}"
        }]
    )

def _make_direct_response(content: str) -> AIMessage:
    """Crea un AIMessage de respuesta directa (sin tool calls)."""
    return AIMessage(content=content, tool_calls=[])


def _rag_side_effect(doc_path: str, query: str, k: int = 5) -> str:
    """
    Side effect para mock de rag_service.search_knowledge.
    Devuelve el contenido REAL del archivo KB correspondiente.
    """
    key = DOC_PATH_TO_KEY.get(doc_path)
    if key:
        return KB_CONTENT[key]
    return f"[ERROR] Documento no encontrado: {doc_path}"


def _build_agent_mock(tool_decision_response: AIMessage, final_response: str = "OK"):
    """
    Construye los mocks necesarios para el InfoAgent:
    - llama_client.client.bind_tools → llm con .ainvoke que devuelve la decisión
    - llama_client.ainvoke → respuesta final del LLM (generación RAG)
    - rag_service.search_knowledge → contenido real del KB
    """
    mock_client = MagicMock()
    mock_llm_with_tools = MagicMock()
    mock_llm_with_tools.ainvoke = AsyncMock(return_value=tool_decision_response)
    mock_client.bind_tools.return_value = mock_llm_with_tools

    mock_final_llm = AsyncMock(
        return_value=MagicMock(content=final_response)
    )

    return mock_client, mock_final_llm


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURE PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def info_agent():
    """InfoAgent con vector store y LLM client mockeados (sin DB real)."""
    with patch("rag.vector_store.PgVectorStore.__init__", return_value=None), \
         patch("rag.rag_service.pg_vector_store"), \
         patch("llm_client.ChatOpenAI"):
        from agents.InfoAgent.info_agent import InfoAgent as _InfoAgent
        return _InfoAgent()


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 1: INTEGRIDAD DEL CONTENIDO DE LA KB ────────────────────────────
# Tests de caja negra que NO requieren el agente: verifican los archivos raw.
# ═══════════════════════════════════════════════════════════════════════════════

class TestKBContentIntegrity:
    """
    BLOQUE 1 — Integridad del contenido en los archivos de Knowledge Base.
    Estos tests son independientes del agente y validan los datos en reposo.
    """

    def test_numero_correcto_en_info_pagos(self):
        """KB Pagos debe contener el número correcto 604 444 63 64."""
        assert NUMERO_CORRECTO in KB_CONTENT["info_pagos"], (
            f"[ANOMALÍA] '{NUMERO_CORRECTO}' NO encontrado en info_pagos_online.txt"
        )

    def test_numero_correcto_en_caja(self):
        """KB Caja debe contener el número correcto 604 444 63 64."""
        assert NUMERO_CORRECTO in KB_CONTENT["soporte_caja"], (
            f"[ANOMALÍA] '{NUMERO_CORRECTO}' NO encontrado en soporte_caja_pagos.txt"
        )

    def test_numero_correcto_en_contabilidad(self):
        """KB Contabilidad debe contener el número correcto 604 444 63 64."""
        assert NUMERO_CORRECTO in KB_CONTENT["soporte_contabilidad"], (
            f"[ANOMALÍA] '{NUMERO_CORRECTO}' NO encontrado en soporte_contabilidad_facturas.txt"
        )

    def test_numero_correcto_en_contratos(self):
        """KB Contratos debe contener el número correcto 604 444 63 64."""
        assert NUMERO_CORRECTO in KB_CONTENT["soporte_contratos"], (
            f"[ANOMALÍA] '{NUMERO_CORRECTO}' NO encontrado en soporte_contratos_terminacion.txt"
        )

    def test_numero_correcto_en_institucional(self):
        """KB Institucional debe contener el número correcto 604 444 63 64."""
        assert NUMERO_CORRECTO in KB_CONTENT["info_institucional"], (
            f"[ANOMALÍA] '{NUMERO_CORRECTO}' NO encontrado en informacion_institucional.txt"
        )

    def test_url_pagos_en_info_pagos(self):
        """KB Pagos DEBE contener la URL directa de pagos online."""
        assert URL_PAGOS in KB_CONTENT["info_pagos"], (
            f"[ANOMALÍA] URL de pagos NO encontrada en info_pagos_online.txt\n"
            f"Esperada: {URL_PAGOS}"
        )

    def test_url_pagos_en_caja(self):
        """KB Caja DEBE contener la URL directa de pagos online."""
        assert URL_PAGOS in KB_CONTENT["soporte_caja"], (
            f"[ANOMALÍA] URL de pagos NO encontrada en soporte_caja_pagos.txt\n"
            f"Esperada: {URL_PAGOS}"
        )

    def test_sin_numero_incorrecto_en_todos_los_archivos(self):
        """Ningún archivo KB debe contener el número incorrecto '604 444 6364'."""
        archivos_con_error = []
        for key, content in KB_CONTENT.items():
            if NUMERO_INCORRECTO in content:
                archivos_con_error.append(key)
        assert not archivos_con_error, (
            f"[ANOMALÍA] Número INCORRECTO '{NUMERO_INCORRECTO}' encontrado en: "
            f"{archivos_con_error}"
        )

    @pytest.mark.parametrize("numero_obsoleto", NUMEROS_OBSOLETOS)
    def test_sin_numeros_obsoletos_en_kb(self, numero_obsoleto: str):
        """Ningún archivo KB debe contener números de teléfono obsoletos."""
        archivos_con_obsoleto = []
        for key, content in KB_CONTENT.items():
            if numero_obsoleto in content:
                archivos_con_obsoleto.append(key)
        assert not archivos_con_obsoleto, (
            f"[ANOMALÍA] Número OBSOLETO '{numero_obsoleto}' encontrado en: "
            f"{archivos_con_obsoleto}"
        )

    def test_info_pagos_no_esta_corrupto_utf16(self):
        """KB Pagos no debe tener texto con caracteres separados por espacios (corrupción UTF-16)."""
        content = KB_CONTENT["info_pagos"]
        # Detección de patrón UTF-16 corrupto: letras sueltas separadas por espacios
        # Ejemplo: "P S E   d e   D a v i v i e n d a"
        patron_corrupto = re.compile(r'\b[A-Za-z] [A-Za-z] [A-Za-z] [A-Za-z]\b')
        assert not patron_corrupto.search(content), (
            "[ANOMALÍA] info_pagos_online.txt parece tener corrupción de encoding UTF-16. "
            "El archivo tiene caracteres separados por espacios."
        )

    def test_link_libertador_en_kb(self):
        """KB Libertador debe contener el link de estudio de crédito."""
        link = "https://analisisweb.ellibertador.co/estudio-digital/datos-basicos/natural"
        assert link in KB_CONTENT["info_libertador"], (
            f"[ANOMALÍA] Link del Libertador no encontrado en info_estudios_libertador.txt"
        )

    def test_municipios_cobertura_completos(self):
        """KB Cobertura debe listar los 10 municipios del Área Metropolitana."""
        municipios_esperados = [
            "Medellín", "Barbosa", "Girardota", "Copacabana", "Bello",
            "Itagüí", "Sabaneta", "Envigado", "La Estrella", "Caldas"
        ]
        content = KB_CONTENT["info_cobertura"]
        faltantes = [m for m in municipios_esperados if m not in content]
        assert not faltantes, (
            f"[ANOMALÍA] Municipios faltantes en info_cobertura_propiedades.txt: {faltantes}"
        )

    def test_codigo_bancolombia_en_pagos(self):
        """KB Pagos debe contener el código de Bancolombia (86582)."""
        assert "86582" in KB_CONTENT["info_pagos"], (
            "[ANOMALÍA] Código Bancolombia '86582' no encontrado en info_pagos_online.txt"
        )

    def test_departamentos_con_numeros_correctos(self):
        """KB Departamentos debe tener los números de contacto conocidos."""
        numeros_esperados = {
            "320 609 2896": "Administraciones",
            "321 789 86 79": "Jurídico",
            "323 508 18 84": "Servicios Públicos",
            "323 327 7132": "Reparaciones",
        }
        content = KB_CONTENT["soporte_departamentos"]
        for numero, dept in numeros_esperados.items():
            assert numero in content, (
                f"[ANOMALÍA] Número de {dept} '{numero}' no encontrado en soporte_departamentos.txt"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 2: EL LIBERTADOR — BYPASS RAG ──────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestElLibertadorBypass:
    """
    BLOQUE 2 — El Libertador usa una respuesta FIJA que bypasea el RAG.
    Verificar que el agente nunca llama al RAG para estas preguntas.
    """

    PREGUNTAS_LIBERTADOR = [
        "¿Qué se necesita para hacer el estudio de crédito?",
        "cuáles son los requisitos para el arriendo?",
        "cómo hago el estudio de arriendo?",
        "necesito información del libertador",
        "quiero hacer el estudio crédito para arrendar",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pregunta", PREGUNTAS_LIBERTADOR)
    async def test_libertador_no_llama_al_rag(self, info_agent, pregunta: str):
        """Para preguntas del Libertador, el RAG NO debe llamarse."""
        with patch("agents.InfoAgent.info_agent.rag_service.search_knowledge") as mock_rag, \
             patch("agents.InfoAgent.info_agent.llama_client.client") as mock_client:

            response = await info_agent.process_info_query(pregunta)

            mock_rag.assert_not_called()
            mock_client.bind_tools.assert_not_called()
            assert isinstance(response, str)
            assert len(response) > 10

    @pytest.mark.asyncio
    async def test_libertador_respuesta_contiene_link(self, info_agent):
        """La respuesta del Libertador debe contener el link del proceso digital."""
        link_esperado = "https://analisisweb.ellibertador.co/estudio-digital/datos-basicos/natural"
        with patch("agents.InfoAgent.info_agent.rag_service.search_knowledge"), \
             patch("agents.InfoAgent.info_agent.llama_client.client"):

            response = await info_agent.process_info_query(
                "¿qué requisitos necesito para el estudio de crédito del libertador?"
            )
            assert link_esperado in response, (
                f"[ANOMALÍA] Link del Libertador no aparece en la respuesta fija.\n"
                f"Respuesta obtenida: {response[:200]}"
            )

    @pytest.mark.asyncio
    async def test_libertador_respuesta_es_gratuita(self, info_agent):
        """La respuesta del Libertador debe indicar que el proceso NO tiene costo."""
        with patch("agents.InfoAgent.info_agent.rag_service.search_knowledge"), \
             patch("agents.InfoAgent.info_agent.llama_client.client"):

            response = await info_agent.process_info_query("requisitos arriendo estudio crédito")
            assert "costo" in response.lower() or "gratis" in response.lower() or "gratuito" in response.lower(), (
                "[ANOMALÍA] La respuesta del Libertador no menciona que el proceso es gratuito."
            )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 3: ENRUTAMIENTO DE TOOLS ────────────────────────────────────────
# Verificar que el LLM elige el tool correcto para cada tipo de pregunta
# ═══════════════════════════════════════════════════════════════════════════════

class TestRAGToolRouting:
    """
    BLOQUE 3 — Verificación del enrutamiento de tools.
    El LLM debe elegir 'info_institucional' para preguntas de empresa,
    y 'soporte_contacto' para preguntas de soporte/pagos.
    """

    @pytest.mark.asyncio
    async def test_pregunta_institucional_usa_tool_info(self, info_agent):
        """'¿Cuándo abrieron?' → tool info_institucional."""
        tool_response = _make_tool_response("info_institucional", "historia empresa")
        mock_client, mock_final_llm = _build_agent_mock(
            tool_response, "Inmobiliaria Proteger fue fundada en 1990."
        )
        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke", mock_final_llm), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect) as mock_rag:

            response = await info_agent.process_info_query("¿Cuándo fue fundada la inmobiliaria?")

            mock_client.bind_tools.assert_called_once()
            # info_institucional busca en 3 documentos
            assert mock_rag.call_count == 3
            assert isinstance(response, str)

    @pytest.mark.asyncio
    async def test_pregunta_pagos_usa_tool_soporte(self, info_agent):
        """'¿Cómo pago mi arriendo?' → tool soporte_contacto."""
        tool_response = _make_tool_response("soporte_contacto", "métodos de pago")
        mock_client, mock_final_llm = _build_agent_mock(
            tool_response, "Puede pagar por PSE, Bancolombia APP o en efectivo."
        )
        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke", mock_final_llm), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect) as mock_rag:

            response = await info_agent.process_info_query("¿Cómo puedo pagar mi arriendo?")

            mock_client.bind_tools.assert_called_once()
            # soporte_contacto busca en 5 documentos
            assert mock_rag.call_count == 5
            assert isinstance(response, str)

    @pytest.mark.asyncio
    async def test_pregunta_cobertura_usa_tool_info(self, info_agent):
        """'¿Tienen propiedades en Sabaneta?' → tool info_institucional."""
        tool_response = _make_tool_response("info_institucional", "cobertura municipal")
        mock_client, mock_final_llm = _build_agent_mock(
            tool_response, "Sí, operamos en Sabaneta."
        )
        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke", mock_final_llm), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect) as mock_rag:

            await info_agent.process_info_query("¿Tienen apartamentos en Sabaneta?")
            assert mock_rag.call_count == 3

    @pytest.mark.asyncio
    async def test_saludo_no_usa_rag(self, info_agent):
        """'Hola, buenos días' → respuesta directa SIN RAG."""
        direct_response = _make_direct_response("¡Hola! Soy Sofía, ¿en qué puedo ayudarte?")
        mock_client, _ = _build_agent_mock(direct_response)
        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge") as mock_rag:

            response = await info_agent.process_info_query("Hola, buenos días")

            mock_rag.assert_not_called()
            assert "Sofía" in response or "hola" in response.lower() or len(response) > 5

    @pytest.mark.asyncio
    async def test_tool_count_info_institucional(self, info_agent):
        """info_institucional debe buscar exactamente en 3 documentos."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP
        assert len(TOOL_DOCUMENT_MAP["info_institucional"]) == 3, (
            "[ANOMALÍA] info_institucional debe mapear exactamente 3 documentos."
        )

    @pytest.mark.asyncio
    async def test_tool_count_soporte_contacto(self, info_agent):
        """soporte_contacto debe buscar exactamente en 5 documentos."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP
        assert len(TOOL_DOCUMENT_MAP["soporte_contacto"]) == 5, (
            "[ANOMALÍA] soporte_contacto debe mapear exactamente 5 documentos."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 4: PAGOS — TEST CRÍTICO ─────────────────────────────────────────
# El más importante: verifica que el número Y la URL de pagos llegan al LLM
# ═══════════════════════════════════════════════════════════════════════════════

class TestPagosInformation:
    """
    BLOQUE 4 — Información de Pagos (TEST CRÍTICO).
    Valida que cuando un cliente pregunta por pagos:
      1. Se usa la tool 'soporte_contacto'
      2. El contexto enviado al LLM contiene el número correcto
      3. El contexto enviado al LLM contiene la URL de pagos
      4. No se envía el número incorrecto al LLM
    """

    PREGUNTAS_PAGO = [
        "¿Cómo pago mi arriendo?",
        "¿Cuáles son los medios de pago?",
        "¿Puedo pagar en línea?",
        "¿Cuál es el número de caja para pagar?",
        "¿Cómo hago el pago por PSE?",
        "¿Dónde pago el arriendo con Bancolombia?",
        "¿A qué número escribo para pagar?",
        "necesito pagar mi renta este mes",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pregunta", PREGUNTAS_PAGO)
    async def test_contexto_pago_contiene_numero_correcto(
        self, info_agent, pregunta: str
    ):
        """El contexto RAG enviado al LLM debe contener 604 444 63 64."""
        tool_response = _make_tool_response("soporte_contacto", "pagos")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="Puede pagar por PSE o Bancolombia.")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query(pregunta)

        full_context = " ".join(captured_context)
        assert NUMERO_CORRECTO in full_context, (
            f"[ANOMALÍA CRÍTICA] '{NUMERO_CORRECTO}' NO llegó al LLM para pregunta: '{pregunta}'\n"
            f"Contexto enviado (primeros 300 chars): {full_context[:300]}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pregunta", PREGUNTAS_PAGO)
    async def test_contexto_pago_contiene_url_pagos(
        self, info_agent, pregunta: str
    ):
        """El contexto RAG enviado al LLM debe contener la URL de pagos online."""
        tool_response = _make_tool_response("soporte_contacto", "pagos")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="Puede pagar en línea.")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query(pregunta)

        full_context = " ".join(captured_context)
        assert URL_PAGOS in full_context, (
            f"[ANOMALÍA CRÍTICA] URL de pagos NO llegó al LLM para pregunta: '{pregunta}'\n"
            f"URL esperada: {URL_PAGOS}\n"
            f"Contexto enviado (primeros 400 chars): {full_context[:400]}"
        )

    @pytest.mark.asyncio
    async def test_contexto_pago_no_contiene_numero_incorrecto(self, info_agent):
        """El contexto RAG NO debe contener el número incorrecto '604 444 6364'."""
        tool_response = _make_tool_response("soporte_contacto", "pagos")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query("¿Cómo pago mi arriendo?")

        full_context = " ".join(captured_context)
        assert NUMERO_INCORRECTO not in full_context, (
            f"[ANOMALÍA CRÍTICA] Número INCORRECTO '{NUMERO_INCORRECTO}' "
            f"está llegando al LLM. El RAG devuelve datos corruptos."
        )

    @pytest.mark.asyncio
    async def test_contexto_pago_contiene_pse_bancolombia(self, info_agent):
        """
        PSE y Bancolombia están en info_pagos_online.txt → tool info_institucional.
        Cuando el LLM elige info_institucional para preguntas de métodos de pago,
        el contexto DEBE contener PSE, Bancolombia y el código 86582.
        """
        # info_pagos_online.txt está en info_institucional, NO en soporte_contacto
        tool_response = _make_tool_response("info_institucional", "métodos de pago")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query("¿Qué métodos de pago tienen?")

        full_context = " ".join(captured_context)
        assert "PSE" in full_context, "[ANOMALÍA] PSE no está en contexto de info_institucional"
        assert "Bancolombia" in full_context, "[ANOMALÍA] Bancolombia no está en contexto"
        assert "86582" in full_context, "[ANOMALÍA] Código Bancolombia (86582) no está en contexto"

    @pytest.mark.asyncio
    async def test_disenio_soporte_caja_sin_detalle_pse(self, info_agent):
        """
        ⚠️ LIMITACIÓN DE DISEÑO DETECTADA:
        soporte_contacto NO incluye info_pagos_online.txt.
        Si el LLM elige soporte_contacto para 'métodos de pago',
        el contexto tendrá el número de Caja pero NO los detalles PSE/Bancolombia.
        Este test documenta y valida este comportamiento conocido.
        """
        tool_response = _make_tool_response("soporte_contacto", "métodos de pago")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query("¿Qué métodos de pago tienen?")

        full_context = " ".join(captured_context)
        # soporte_contacto SÍ tiene el número correcto (de soporte_caja_pagos.txt)
        assert NUMERO_CORRECTO in full_context, (
            "[ANOMALÍA] soporte_contacto debería tener el número de Caja en contexto."
        )
        # LIMITACIÓN CONOCIDA: PSE/Bancolombia no está en soporte_contacto
        # Si esto falla en el futuro, significa que se añadió info_pagos_online.txt
        # a soporte_contacto (mejora recomendada)
        assert "86582" not in full_context, (
            "[INFO] Código Bancolombia ahora está en soporte_contacto — limitación resuelta."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 5: INFORMACIÓN INSTITUCIONAL ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestInformacionInstitucional:
    """
    BLOQUE 5 — Consultas sobre la empresa (historia, horarios, cobertura).
    """

    PREGUNTAS_INSTITUCIONAL = [
        ("¿Cuándo fue fundada la inmobiliaria?", "1990"),
        ("¿Cuál es el horario de atención?", "8:30"),
        ("¿Dónde están ubicados?", "Envigado"),
        ("¿En qué municipios operan?", "Sabaneta"),
        ("¿Tienen propiedades en Medellín?", "Medellín"),
        ("¿Cuál es la misión de la empresa?", "Misión"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pregunta,keyword_esperado", PREGUNTAS_INSTITUCIONAL)
    async def test_contexto_institucional_contiene_info_clave(
        self, info_agent, pregunta: str, keyword_esperado: str
    ):
        """El contexto institucional debe contener la información esperada."""
        tool_response = _make_tool_response("info_institucional", pregunta[:20])
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="Respuesta institucional")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query(pregunta)

        full_context = " ".join(captured_context)
        assert keyword_esperado in full_context, (
            f"[ANOMALÍA] '{keyword_esperado}' no encontrado en contexto para: '{pregunta}'\n"
            f"Contexto (300 chars): {full_context[:300]}"
        )

    @pytest.mark.asyncio
    async def test_contexto_institucional_no_da_precios_especificos(self, info_agent):
        """La KB debe advertir que los precios de muestra NO son actuales."""
        tool_response = _make_tool_response("info_institucional", "precios")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query("¿Cuánto cuesta arrendar un apartamento?")

        full_context = " ".join(captured_context)
        # El KB debe contener la advertencia sobre precios no actuales
        assert "ADVERTENCIA" in full_context or "CRÍTICA" in full_context or "muestra" in full_context.lower(), (
            "[ANOMALÍA] KB no contiene advertencia de que los precios son de muestra y NO actuales."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 6: SOPORTE DE DEPARTAMENTOS ─────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestSoporteDepartamentos:
    """
    BLOQUE 6 — Consultas de soporte a departamentos específicos.
    Verifica que el contexto correcto llega para cada área de soporte.
    """

    CASOS_SOPORTE = [
        ("¿A quién llamo por un daño en el apartamento?",   "323 327 7132",  "Reparaciones"),
        ("Tengo problemas con la factura EPM",              "323 508 18 84", "Servicios Públicos"),
        ("Quiero hablar con el abogado",                    "321 789 86 79", "Jurídico"),
        ("¿Cuánto es la administración del conjunto?",      "320 609 2896",  "Administraciones"),
        ("Quiero saber sobre mi certificado tributario",    NUMERO_CORRECTO, "Contabilidad"),
        ("¿Cómo termino mi contrato de arrendamiento?",     NUMERO_CORRECTO, "Contratos"),
        ("Necesito certificado de ingresos del arriendo",   NUMERO_CORRECTO, "Caja"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pregunta,numero_esperado,dept", CASOS_SOPORTE)
    async def test_soporte_devuelve_numero_correcto_por_departamento(
        self, info_agent, pregunta: str, numero_esperado: str, dept: str
    ):
        """Cada consulta de soporte debe resultar en el número de contacto correcto en el contexto."""
        tool_response = _make_tool_response("soporte_contacto", dept.lower())
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content=f"Contacte a {dept}")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query(pregunta)

        full_context = " ".join(captured_context)
        assert numero_esperado in full_context, (
            f"[ANOMALÍA] Número de {dept} '{numero_esperado}' no encontrado en contexto.\n"
            f"Pregunta: '{pregunta}'\n"
            f"Contexto (400 chars): {full_context[:400]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 7: CONTRATOS ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestContratos:
    """
    BLOQUE 7 — Consultas sobre contratos de arrendamiento.
    """

    @pytest.mark.asyncio
    async def test_contexto_contratos_menciona_3_meses_anticipacion(self, info_agent):
        """KB Contratos debe indicar que se necesitan 3 meses de anticipación para terminar."""
        tool_response = _make_tool_response("soporte_contacto", "terminación contrato")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query("¿Cuánto tiempo antes debo avisar que me voy del apartamento?")

        full_context = " ".join(captured_context)
        assert "3 MESES" in full_context or "3 meses" in full_context.lower(), (
            "[ANOMALÍA] KB no menciona los 3 meses de anticipación requeridos para terminación."
        )

    @pytest.mark.asyncio
    async def test_contexto_contratos_tiene_numero_correcto(self, info_agent):
        """KB Contratos debe contener el número correcto 604 444 63 64."""
        tool_response = _make_tool_response("soporte_contacto", "contratos")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query("¿Cómo termino mi contrato?")

        full_context = " ".join(captured_context)
        assert NUMERO_CORRECTO in full_context


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 8: DETECCIÓN DE ANOMALÍAS EN RUNTIME ────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnomalias:
    """
    BLOQUE 8 — Detección de anomalías en runtime: números obsoletos, errores de RAG.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("numero_obsoleto", NUMEROS_OBSOLETOS)
    async def test_rag_service_valida_numeros_obsoletos(
        self, info_agent, numero_obsoleto: str
    ):
        """
        El RAG service debe rechazar contextos que contienen números obsoletos.
        Simula que el vector store devuelve un chunk con el número obsoleto.
        """
        with patch("rag.vector_store.PgVectorStore.__init__", return_value=None), \
             patch("rag.rag_service.pg_vector_store") as mock_pg:

            from rag.rag_service import RAGService
            svc = RAGService()
            # Inyectar un chunk con número obsoleto para triggear la validación
            contenido_con_obsoleto = f"Contacto de Contratos: {numero_obsoleto}. Llame para más info."

            try:
                svc._validate_response_no_obsolete_numbers(contenido_con_obsoleto)
                pytest.fail(
                    f"[ANOMALÍA] RAG service NO rechazó el número obsoleto '{numero_obsoleto}'. "
                    f"Debería lanzar RuntimeError."
                )
            except RuntimeError:
                pass  # Comportamiento correcto: rechaza el número obsoleto

    @pytest.mark.asyncio
    async def test_tool_inexistente_devuelve_error_graceful(self, info_agent):
        """Si el LLM invoca una tool desconocida, el agente debe manejar el error."""
        tool_response = _make_tool_response("tool_inexistente_xyz", "tema")
        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   new_callable=AsyncMock,
                   return_value=MagicMock(content="Respuesta fallback")), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            response = await info_agent.process_info_query("¿Algo muy raro?")
            # El agente no debe explotar — debe devolver algo
            assert isinstance(response, str)

    @pytest.mark.asyncio
    async def test_rag_error_no_rompe_el_agente(self, info_agent):
        """Si el RAG lanza excepción, el agente debe retornar mensaje de error educado."""
        tool_response = _make_tool_response("soporte_contacto", "pagos")
        mock_client, _ = _build_agent_mock(tool_response)

        def rag_que_falla(doc_path, query, k=5):
            raise ConnectionError("pgvector no disponible")

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=rag_que_falla):

            response = await info_agent.process_info_query("¿Cómo pago?")
            assert isinstance(response, str)
            # No debe devolver traceback al usuario
            assert "Traceback" not in response
            assert "ConnectionError" not in response


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 9: COBERTURA GEOGRÁFICA ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoberturaGeografica:
    """
    BLOQUE 9 — Consultas de cobertura geográfica.
    """

    MUNICIPIOS_CUBIERTOS = [
        "Medellín", "Bello", "Itagüí", "Sabaneta",
        "Envigado", "La Estrella", "Caldas",
        "Copacabana", "Girardota", "Barbosa"
    ]

    CIUDADES_FUERA_COBERTURA = ["Bogotá", "Cali", "Cartagena", "Barranquilla"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("municipio", MUNICIPIOS_CUBIERTOS)
    async def test_municipio_cubierto_aparece_en_contexto(
        self, info_agent, municipio: str
    ):
        """El contexto de cobertura debe incluir cada municipio del Área Metropolitana."""
        tool_response = _make_tool_response("info_institucional", "cobertura")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query(f"¿Tienen propiedades en {municipio}?")

        full_context = " ".join(captured_context)
        assert municipio in full_context, (
            f"[ANOMALÍA] '{municipio}' no aparece en contexto de cobertura."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ciudad", CIUDADES_FUERA_COBERTURA)
    async def test_ciudades_fuera_cobertura_tienen_advertencia(
        self, info_agent, ciudad: str
    ):
        """La KB debe advertir que ciudades fuera del Área Metro no son cubiertas."""
        tool_response = _make_tool_response("info_institucional", "cobertura")
        captured_context: List[str] = []

        async def capture_final_call(messages_rag):
            for msg in messages_rag:
                captured_context.append(msg.content)
            return MagicMock(content="OK")

        mock_client, _ = _build_agent_mock(tool_response)

        with patch("agents.InfoAgent.info_agent.llama_client.client", mock_client), \
             patch("agents.InfoAgent.info_agent.llama_client.ainvoke",
                   side_effect=capture_final_call), \
             patch("agents.InfoAgent.info_agent.rag_service.search_knowledge",
                   side_effect=_rag_side_effect):

            await info_agent.process_info_query(f"¿Tienen propiedades en {ciudad}?")

        full_context = " ".join(captured_context)
        assert "No operamos" in full_context or "IMPORTANTE" in full_context or "fuera" in full_context.lower(), (
            f"[ANOMALÍA] KB no advierte sobre la NO cobertura en '{ciudad}'."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ── BLOQUE 10: VERIFICACIÓN DEL TOOL_DOCUMENT_MAP ──────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolDocumentMapIntegrity:
    """
    BLOQUE 10 — Verificar que los 8 documentos están mapeados y existen en disco.
    """

    def test_todos_los_documentos_existen_en_disco(self):
        """Todos los paths definidos en TOOL_DOCUMENT_MAP deben existir como archivos."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP

        todos_los_paths = (
            TOOL_DOCUMENT_MAP["info_institucional"] +
            TOOL_DOCUMENT_MAP["soporte_contacto"]
        )

        faltantes = []
        for path in todos_los_paths:
            if not Path(path).exists():
                faltantes.append(path)

        assert not faltantes, (
            f"[ANOMALÍA CRÍTICA] Documentos definidos en TOOL_DOCUMENT_MAP "
            f"pero NO existen en disco: {faltantes}"
        )

    def test_total_documentos_es_8(self):
        """El total de documentos mapeados debe ser exactamente 8."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP
        total = sum(len(v) for v in TOOL_DOCUMENT_MAP.values())
        assert total == 8, (
            f"[ANOMALÍA] Se esperan 8 documentos en TOOL_DOCUMENT_MAP, hay {total}."
        )

    def test_info_pagos_esta_mapeado_en_info_institucional(self):
        """info_pagos_online.txt debe estar en el tool 'info_institucional'."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP
        paths = TOOL_DOCUMENT_MAP["info_institucional"]
        assert any("info_pagos_online" in p for p in paths), (
            "[ANOMALÍA] info_pagos_online.txt no está mapeado en 'info_institucional'."
        )

    def test_soporte_caja_esta_mapeado_en_soporte_contacto(self):
        """soporte_caja_pagos.txt debe estar en el tool 'soporte_contacto'."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP
        paths = TOOL_DOCUMENT_MAP["soporte_contacto"]
        assert any("soporte_caja_pagos" in p for p in paths), (
            "[ANOMALÍA] soporte_caja_pagos.txt no está mapeado en 'soporte_contacto'."
        )

    def test_todos_los_documentos_son_utf8_valido(self):
        """Todos los archivos KB deben poder leerse como UTF-8 (no UTF-16 corrupto)."""
        from agents.InfoAgent.info_agent import TOOL_DOCUMENT_MAP
        todos_los_paths = (
            TOOL_DOCUMENT_MAP["info_institucional"] +
            TOOL_DOCUMENT_MAP["soporte_contacto"]
        )
        corruptos = []
        for path_str in todos_los_paths:
            path = Path(path_str)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    # Detectar patrón UTF-16: caracteres alternados con espacios
                    patron = re.compile(r'([A-Za-z] ){5,}')
                    if patron.search(content):
                        corruptos.append(path_str)
                except UnicodeDecodeError:
                    corruptos.append(f"{path_str} (UnicodeDecodeError)")
        assert not corruptos, (
            f"[ANOMALÍA] Archivos con posible corrupción de encoding: {corruptos}"
        )
