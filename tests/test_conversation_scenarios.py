# tests/test_conversation_scenarios.py
"""
Script de pruebas para simular conversaciones reales con el sistema multi-agente.
Envía datos al CRM (HubSpot) a través de la API desplegada en Railway.

Uso:
    python -m tests.test_conversation_scenarios
    python -m tests.test_conversation_scenarios --scenario 3
    python -m tests.test_conversation_scenarios --local
    python -m tests.test_conversation_scenarios --list
"""

import asyncio
import argparse
import httpx
import random
import string
from datetime import datetime
from typing import List, Dict, Any, Optional

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

# URL de la API (Railway production)
API_URL_PRODUCTION = "https://agenteconversacionalpractica-production.up.railway.app"
API_URL_LOCAL = "http://localhost:8000"

# Timeout para requests
REQUEST_TIMEOUT = 30.0

# Delay entre mensajes (simula conversación real)
MESSAGE_DELAY = 1.5


def generate_unique_phone() -> str:
    """Genera un número de teléfono único para evitar duplicados en HubSpot."""
    random_digits = ''.join(random.choices(string.digits, k=6))
    return f"+5730012{random_digits}"


# ═══════════════════════════════════════════════════════════════════════════════
# ESCENARIOS DE CONVERSACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

def get_scenarios() -> List[Dict[str, Any]]:
    """
    Retorna los escenarios con teléfonos únicos generados dinámicamente.
    Esto evita conflictos de duplicados en HubSpot.
    """
    return [
        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 1: Joven profesional - Arriendo apartamento - Urgente
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 1,
            "nombre": "Joven profesional urgente",
            "descripcion": "Profesional de 28 años que necesita apartamento urgente",
            "perfil": {
                "nombre": "Carolina Mendez",
                "edad": 28,
                "ocupacion": "Ingeniera de software",
                "canal_origen": "Instagram Ads",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Hola! Vi su anuncio en Instagram. Necesito un apartamento urgente",
                "Busco arriendo en El Poblado o Laureles",
                "Mi presupuesto es de 2 a 3 millones mensuales",
                "Necesito 2 habitaciones y ojalá con parqueadero",
                "Lo necesito para este mes, me trasladan de trabajo",
                "Carolina Mendez",
            ],
            "datos_esperados": {
                "nombre": "Carolina Mendez",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "arriendo",
                "ubicacion": "El Poblado/Laureles",
                "presupuesto": "2-3 millones",
                "tiempo": "este mes",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 2: Familia - Compra casa - Sin prisa
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 2,
            "nombre": "Familia comprando casa",
            "descripcion": "Padre de familia de 45 años buscando casa, sin afán",
            "perfil": {
                "nombre": "Roberto Jiménez Mora",
                "edad": 45,
                "ocupacion": "Gerente comercial",
                "canal_origen": "Referido",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Buenos días, un amigo me recomendó su inmobiliaria",
                "Estoy buscando una casa para mi familia, somos 4",
                "Queremos comprar, tenemos un presupuesto de unos 500 millones",
                "Nos interesa Envigado o Sabaneta",
                "No tenemos afán, queremos encontrar la indicada",
                "3 habitaciones mínimo, con patio sería ideal",
                "Roberto Jiménez Mora",
            ],
            "datos_esperados": {
                "nombre": "Roberto Jiménez Mora",
                "tipo_propiedad": "casa",
                "tipo_operacion": "compra",
                "presupuesto": "500 millones",
                "tiempo": "sin afán",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 3: Adulto mayor - Apartaestudio - Mensajes cortos
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 3,
            "nombre": "Adulto mayor - comunicación breve",
            "descripcion": "Señora de 68 años que escribe mensajes cortos",
            "perfil": {
                "nombre": "María Elena Rojas",
                "edad": 68,
                "ocupacion": "Pensionada",
                "canal_origen": "WhatsApp directo",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Buenas tardes",
                "Busco apartaestudio",
                "En arriendo",
                "Zona Belén o Laureles",
                "Hasta 1 millon 200",
                "Para vivir sola",
                "Lo necesito pronto",
                "María Elena Rojas",
            ],
            "datos_esperados": {
                "nombre": "María Elena Rojas",
                "tipo_propiedad": "apartaestudio",
                "tipo_operacion": "arriendo",
                "presupuesto": "1.200.000",
                "tiempo": "pronto",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 4: Inversionista - Local comercial - Formal
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 4,
            "nombre": "Inversionista formal",
            "descripcion": "Empresario de 52 años buscando local comercial",
            "perfil": {
                "nombre": "Andrés Felipe Castañeda",
                "edad": 52,
                "ocupacion": "Empresario / Inversionista",
                "canal_origen": "Google Ads",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Buenas tardes. Estoy interesado en adquirir un local comercial.",
                "Busco compra, preferiblemente en zona comercial consolidada.",
                "Mi presupuesto está entre 300 y 400 millones de pesos.",
                "Me interesa El Poblado o Envigado por el flujo de personas.",
                "Mínimo 50 metros cuadrados, con vitrina a la calle.",
                "No tengo urgencia, puedo esperar una buena oportunidad.",
                "Andrés Felipe Castañeda",
            ],
            "datos_esperados": {
                "nombre": "Andrés Felipe Castañeda",
                "tipo_propiedad": "local comercial",
                "tipo_operacion": "compra",
                "presupuesto": "300-400 millones",
                "tiempo": "sin urgencia",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 5: Estudiante - Bajo presupuesto
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 5,
            "nombre": "Estudiante universitario",
            "descripcion": "Estudiante de 21 años buscando vivienda económica",
            "perfil": {
                "nombre": "Santiago Pérez",
                "edad": 21,
                "ocupacion": "Estudiante universitario",
                "canal_origen": "Facebook",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Hola! Soy estudiante y busco algo económico para arrendar",
                "Vengo de Cali a estudiar en Medellín",
                "Puede ser apartaestudio o habitación",
                "Mi presupuesto máximo es 800 mil pesos",
                "Cerca a la universidad o con buen transporte",
                "Lo necesito para febrero cuando empiezan clases",
                "Santiago Pérez",
            ],
            "datos_esperados": {
                "nombre": "Santiago Pérez",
                "tipo_propiedad": "apartaestudio/habitación",
                "tipo_operacion": "arriendo",
                "presupuesto": "800.000",
                "tiempo": "febrero",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 6: Emprendedor - Oficina
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 6,
            "nombre": "Emprendedor buscando oficina",
            "descripcion": "CEO de Startup de 35 años necesita oficina",
            "perfil": {
                "nombre": "Juliana Torres Vega",
                "edad": 35,
                "ocupacion": "CEO Startup",
                "canal_origen": "LinkedIn",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Hola, busco una oficina para mi empresa de tecnología",
                "Somos un equipo de 8 personas",
                "Preferimos arriendo, más flexible",
                "Zona El Poblado o Laureles, cerca al transporte",
                "Presupuesto entre 4 y 6 millones mensuales",
                "Necesitamos mínimo 80 metros, con sala de reuniones",
                "Para el próximo trimestre idealmente",
                "Juliana Torres Vega",
            ],
            "datos_esperados": {
                "nombre": "Juliana Torres Vega",
                "tipo_propiedad": "oficina",
                "tipo_operacion": "arriendo",
                "presupuesto": "4-6 millones",
                "tiempo": "próximo trimestre",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 7: Pareja joven indecisa
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 7,
            "nombre": "Pareja joven indecisa",
            "descripcion": "Pareja de 30 años comprando su primera vivienda",
            "perfil": {
                "nombre": "Camilo Restrepo",
                "edad": 30,
                "ocupacion": "Contador",
                "canal_origen": "Feria de vivienda",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Hola, los conocí en la feria de vivienda",
                "Mi esposa y yo queremos comprar nuestra primera vivienda",
                "No sabemos si casa o apartamento",
                "Tenemos ahorrado como 100 millones para la cuota inicial",
                "Podríamos pagar cuotas de hasta 3 millones mensuales",
                "Nos gustaría algo con zonas verdes, tenemos un perro",
                "No tenemos afán, apenas estamos mirando",
                "Camilo Restrepo",
            ],
            "datos_esperados": {
                "nombre": "Camilo Restrepo",
                "tipo_propiedad": "casa/apartamento",
                "tipo_operacion": "compra",
                "tiempo": "sin afán",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 8: Propietario quiere vender
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 8,
            "nombre": "Propietario quiere vender",
            "descripcion": "Propietaria de 58 años vendiendo su apartamento",
            "perfil": {
                "nombre": "Gloria Patricia Sánchez",
                "edad": 58,
                "ocupacion": "Docente universitaria",
                "canal_origen": "Página web",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Buenas tardes, quisiera información sobre vender mi apartamento",
                "Tengo un apartamento en El Poblado de 120 metros",
                "Tiene 3 habitaciones, 2 baños y parqueadero doble",
                "Quiero venderlo, mis hijos ya se fueron",
                "Estoy pensando en unos 650 millones",
                "No tengo afán pero tampoco quiero que se demore años",
                "Gloria Patricia Sánchez",
            ],
            "datos_esperados": {
                "nombre": "Gloria Patricia Sánchez",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "venta",
                "presupuesto": "650 millones",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 9: Cliente impaciente - Solo quiere asesor
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 9,
            "nombre": "Cliente impaciente",
            "descripcion": "Cliente que no quiere dar muchos detalles",
            "perfil": {
                "nombre": "Fernando Gómez",
                "edad": 40,
                "ocupacion": "Comerciante",
                "canal_origen": "Llamada",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Necesito hablar con un asesor",
                "Es sobre un apartamento",
                "Prefiero dar los detalles al asesor directamente",
                "Fernando Gómez",
            ],
            "datos_esperados": {
                "nombre": "Fernando Gómez",
                "tipo_propiedad": "apartamento",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 10: Extranjero inversionista
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 10,
            "nombre": "Extranjero inversionista",
            "descripcion": "Estadounidense interesado en invertir en Colombia",
            "perfil": {
                "nombre": "Michael Johnson",
                "edad": 48,
                "ocupacion": "Real Estate Investor",
                "canal_origen": "Referido internacional",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Hello, me recomendaron su inmobiliaria",
                "Estoy looking for investment properties en Medellín",
                "Quiero comprar apartamento para Airbnb",
                "Budget around 200 a 250 millones",
                "Zona turística, El Poblado o Laureles",
                "Need it ready para el próximo año",
                "Michael Johnson",
            ],
            "datos_esperados": {
                "nombre": "Michael Johnson",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "compra",
                "presupuesto": "200-250 millones",
                "tiempo": "próximo año",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 11: Bodega industrial
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 11,
            "nombre": "Empresario busca bodega",
            "descripcion": "Dueño de empresa de logística buscando bodega",
            "perfil": {
                "nombre": "Ricardo Martínez Luna",
                "edad": 55,
                "ocupacion": "Gerente empresa logística",
                "canal_origen": "Portal inmobiliario",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Buenas, necesito una bodega para mi empresa de distribución",
                "Buscamos arriendo, mínimo 500 metros cuadrados",
                "Zona industrial, Itagüí o Bello",
                "Con acceso para tractomulas y zona de cargue",
                "Presupuesto hasta 15 millones mensuales",
                "La necesitamos para dentro de 2 meses máximo",
                "Ricardo Martínez Luna",
            ],
            "datos_esperados": {
                "nombre": "Ricardo Martínez Luna",
                "tipo_propiedad": "bodega",
                "tipo_operacion": "arriendo",
                "presupuesto": "15 millones",
                "tiempo": "2 meses",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 12: Info primero, luego CRM
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 12,
            "nombre": "Info primero, luego CRM",
            "descripcion": "Cliente que primero pregunta y luego pide asesor",
            "perfil": {
                "nombre": "Daniela Vargas",
                "edad": 33,
                "ocupacion": "Abogada",
                "canal_origen": "Google",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "mensajes": [
                "Hola, tengo unas preguntas",
                "Cuál es la comisión que cobran por arriendo?",
                "Gracias. Ahora sí, busco un apartamento en arriendo",
                "Zona El Poblado o Envigado",
                "2 habitaciones, presupuesto de 4 millones",
                "Lo necesito para el próximo mes",
                "Daniela Vargas",
            ],
            "datos_esperados": {
                "nombre": "Daniela Vargas",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "arriendo",
                "presupuesto": "4 millones",
                "tiempo": "próximo mes",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 13: Cliente llega con link de Finca Raíz
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 13,
            "nombre": "Llegada por link - Finca Raíz",
            "descripcion": "Cliente inicia conversación enviando un link de Finca Raíz",
            "perfil": {
                "nombre": "Laura Martínez Ospina",
                "edad": 32,
                "ocupacion": "Diseñadora gráfica",
                "canal_origen": "finca_raiz",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "skip_initial_hola": True,  # NO enviar "hola" inicial, el link ES el primer mensaje
            "mensajes": [
                "https://www.fincaraiz.com.co/apartamento-en-arriendo/medellin/el-poblado/codigo-12345678",
                "Sí, me interesa mucho ese apartamento",
                "Busco algo de 2 habitaciones para vivir sola",
                "Mi presupuesto es hasta 3 millones mensuales",
                "Lo necesito para el próximo mes",
                "Laura Martínez Ospina",
            ],
            "datos_esperados": {
                "nombre": "Laura Martínez Ospina",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "arriendo",
                "ubicacion": "El Poblado",
                "presupuesto": "3 millones",
                "tiempo": "próximo mes",
                "canal_origen": "finca_raiz",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 14: Cliente llega con link de Metrocuadrado
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 14,
            "nombre": "Llegada por link - Metrocuadrado",
            "descripcion": "Cliente inicia conversación enviando un link de Metrocuadrado",
            "perfil": {
                "nombre": "Carlos Eduardo Ríos",
                "edad": 45,
                "ocupacion": "Médico",
                "canal_origen": "metrocuadrado",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "skip_initial_hola": True,
            "mensajes": [
                "https://www.metrocuadrado.com/inmueble/venta-casa-envigado-3-habitaciones",
                "Me interesa esta casa que vi en Metrocuadrado",
                "Quiero comprar, tengo presupuesto de unos 600 millones",
                "Necesito mínimo 3 habitaciones y garaje doble",
                "No tengo afán, estoy mirando opciones",
                "Carlos Eduardo Ríos",
            ],
            "datos_esperados": {
                "nombre": "Carlos Eduardo Ríos",
                "tipo_propiedad": "casa",
                "tipo_operacion": "venta",
                "ubicacion": "Envigado",
                "presupuesto": "600 millones",
                "canal_origen": "metrocuadrado",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 15: Cliente llega con link de Instagram
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 15,
            "nombre": "Llegada por link - Instagram",
            "descripcion": "Cliente inicia enviando link de publicación de Instagram",
            "perfil": {
                "nombre": "Valentina Ochoa",
                "edad": 27,
                "ocupacion": "Community Manager",
                "canal_origen": "instagram",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "skip_initial_hola": True,
            "mensajes": [
                "https://www.instagram.com/p/ABC123xyz/ vi este apartamento en su Instagram!",
                "Está muy lindo, busco arriendo",
                "Zona Laureles o El Poblado",
                "Presupuesto hasta 2.5 millones",
                "Lo necesito ya, es urgente",
                "Valentina Ochoa",
            ],
            "datos_esperados": {
                "nombre": "Valentina Ochoa",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "arriendo",
                "presupuesto": "2.5 millones",
                "tiempo": "urgente",
                "canal_origen": "instagram",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 16: Cliente llega con link de Facebook Marketplace
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 16,
            "nombre": "Llegada por link - Facebook Marketplace",
            "descripcion": "Cliente inicia con link de Facebook Marketplace",
            "perfil": {
                "nombre": "Pedro Antonio Mejía",
                "edad": 38,
                "ocupacion": "Contador",
                "canal_origen": "facebook",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "skip_initial_hola": True,
            "mensajes": [
                "https://www.facebook.com/marketplace/item/987654321 este local me interesa",
                "Busco un local comercial para mi negocio",
                "Prefiero arriendo, zona comercial",
                "Presupuesto de 5 millones mensuales",
                "Lo necesito para dentro de 2 meses",
                "Pedro Antonio Mejía",
            ],
            "datos_esperados": {
                "nombre": "Pedro Antonio Mejía",
                "tipo_propiedad": "local comercial",
                "tipo_operacion": "arriendo",
                "presupuesto": "5 millones",
                "tiempo": "2 meses",
                "canal_origen": "facebook",
            }
        },

        # ─────────────────────────────────────────────────────────────────────
        # ESCENARIO 17: Cliente llega con link de Mercado Libre
        # ─────────────────────────────────────────────────────────────────────
        {
            "id": 17,
            "nombre": "Llegada por link - Mercado Libre",
            "descripcion": "Cliente inicia con link de Mercado Libre Inmuebles",
            "perfil": {
                "nombre": "Andrea Cristina López",
                "edad": 41,
                "ocupacion": "Empresaria",
                "canal_origen": "mercado_libre",
            },
            "session_id": f"whatsapp:{generate_unique_phone()}",
            "skip_initial_hola": True,
            "mensajes": [
                "https://inmuebles.mercadolibre.com.co/apartamento-venta-sabaneta-MLO123456",
                "Hola! Vi este apartamento en Mercado Libre",
                "Quiero comprar para inversión",
                "Tengo 350 millones de presupuesto",
                "Zona Sabaneta o Envigado",
                "No tengo prisa, busco buena oportunidad",
                "Andrea Cristina López",
            ],
            "datos_esperados": {
                "nombre": "Andrea Cristina López",
                "tipo_propiedad": "apartamento",
                "tipo_operacion": "venta",
                "ubicacion": "Sabaneta/Envigado",
                "presupuesto": "350 millones",
                "canal_origen": "mercado_libre",
            }
        },
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTE HTTP
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationClient:
    """Cliente para enviar mensajes a la API."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    async def send_message(self, session_id: str, message: str) -> Dict[str, Any]:
        """
        Envía un mensaje al webhook de la API.

        Args:
            session_id: ID de sesión (formato whatsapp:+57...)
            message: Mensaje del usuario

        Returns:
            Respuesta de la API
        """
        payload = {
            "session_id": session_id,
            "message": message
        }

        try:
            response = await self.client.post(
                f"{self.base_url}/webhook",
                json=payload
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return {
                "error": True,
                "status_code": e.response.status_code,
                "detail": e.response.text
            }
        except Exception as e:
            return {
                "error": True,
                "detail": str(e)
            }

    async def health_check(self) -> bool:
        """Verifica que la API esté disponible."""
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCIONES DE EJECUCIÓN
# ═══════════════════════════════════════════════════════════════════════════════

def print_header(text: str, char: str = "═"):
    """Imprime un encabezado formateado."""
    width = 80
    print(f"\n{char * width}")
    print(f" {text}")
    print(f"{char * width}")


def print_scenario_info(scenario: Dict[str, Any]):
    """Imprime información del escenario."""
    print(f"\n{'─' * 70}")
    print(f"ESCENARIO {scenario['id']}: {scenario['nombre']}")
    print(f"{'─' * 70}")
    print(f"Descripción: {scenario['descripcion']}")
    print(f"\nPerfil del cliente:")
    for key, value in scenario['perfil'].items():
        print(f"  • {key}: {value}")
    print(f"\nSession ID: {scenario['session_id']}")
    print(f"Total mensajes: {len(scenario['mensajes'])}")
    print(f"{'─' * 70}")


async def run_scenario(
    client: ConversationClient,
    scenario: Dict[str, Any],
    verbose: bool = True
) -> Dict[str, Any]:
    """
    Ejecuta un escenario de conversación completo enviando datos al CRM.

    Args:
        client: Cliente HTTP para la API
        scenario: Diccionario con el escenario a ejecutar
        verbose: Si True, imprime cada mensaje y respuesta

    Returns:
        Diccionario con resultados del escenario
    """
    session_id = scenario['session_id']
    results = {
        "scenario_id": scenario['id'],
        "nombre": scenario['nombre'],
        "session_id": session_id,
        "mensajes_enviados": 0,
        "conversacion": [],
        "exito": False,
        "errores": [],
        "datos_esperados": scenario.get('datos_esperados', {})
    }

    if verbose:
        print_scenario_info(scenario)

    try:
        # Mensaje inicial para recibir bienvenida (a menos que el escenario empiece con link)
        skip_initial = scenario.get('skip_initial_hola', False)

        if verbose:
            print("\n[Iniciando conversación...]")

        if not skip_initial:
            response = await client.send_message(session_id, "hola")
            if verbose:
                print(f"\n🤖 SOFÍA: {response.get('response', 'Sin respuesta')[:300]}")
            await asyncio.sleep(MESSAGE_DELAY)
        else:
            if verbose:
                print("\n[Escenario inicia con link - sin mensaje 'hola' previo]")

        # Procesar cada mensaje del escenario
        for i, mensaje in enumerate(scenario['mensajes'], 1):
            if verbose:
                print(f"\n👤 CLIENTE: {mensaje}")

            response = await client.send_message(session_id, mensaje)
            results['mensajes_enviados'] += 1

            if response.get('error'):
                results['errores'].append(f"Turno {i}: {response.get('detail')}")
                if verbose:
                    print(f"❌ ERROR: {response.get('detail')}")
                continue

            results['conversacion'].append({
                "turno": i,
                "usuario": mensaje,
                "agente": response.get('response', ''),
                "status": response.get('status', 'unknown')
            })

            if verbose:
                resp_text = response.get('response', 'Sin respuesta')
                # Truncar respuestas largas
                if len(resp_text) > 400:
                    resp_text = resp_text[:400] + "..."
                print(f"🤖 SOFÍA: {resp_text}")
                print(f"   [Status: {response.get('status', 'N/A')}]")

            # Pausa entre mensajes
            await asyncio.sleep(MESSAGE_DELAY)

        results['exito'] = len(results['errores']) == 0

        if verbose:
            print(f"\n{'─' * 70}")
            print("RESULTADO DEL ESCENARIO:")
            print(f"  • Mensajes enviados: {results['mensajes_enviados']}")
            print(f"  • Errores: {len(results['errores'])}")
            print(f"  • Éxito: {'✅ SÍ' if results['exito'] else '❌ NO'}")
            print(f"\nDatos esperados en HubSpot:")
            for key, value in results['datos_esperados'].items():
                print(f"  • {key}: {value}")
            print(f"{'─' * 70}")

    except Exception as e:
        results['errores'].append(str(e))
        if verbose:
            print(f"\n❌ ERROR CRÍTICO: {e}")

    return results


async def run_all_scenarios(
    base_url: str,
    verbose: bool = True,
    pause_between: bool = True
) -> List[Dict[str, Any]]:
    """Ejecuta todos los escenarios de prueba."""

    scenarios = get_scenarios()

    print_header("PRUEBAS DE CONVERSACIÓN - SISTEMA SOFÍA + HUBSPOT CRM")
    print(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"API URL: {base_url}")
    print(f"Total de escenarios: {len(scenarios)}")

    all_results = []

    async with ConversationClient(base_url) as client:
        # Verificar que la API esté disponible
        print("\n[Verificando conexión con API...]")
        if not await client.health_check():
            print("❌ ERROR: No se puede conectar con la API")
            print(f"   URL: {base_url}")
            return all_results

        print("✅ API disponible")

        for scenario in scenarios:
            results = await run_scenario(client, scenario, verbose)
            all_results.append(results)

            if pause_between and verbose:
                print("\n" + "=" * 80)
                try:
                    input("Presiona Enter para continuar al siguiente escenario...")
                except KeyboardInterrupt:
                    print("\n[Pruebas interrumpidas por el usuario]")
                    break

    # Resumen final
    print_header("RESUMEN DE RESULTADOS")
    exitosos = sum(1 for r in all_results if r['exito'])
    print(f"Escenarios exitosos: {exitosos}/{len(all_results)}")
    print(f"\nDetalle por escenario:")

    for result in all_results:
        status = "✅" if result['exito'] else "❌"
        nombre = result['datos_esperados'].get('nombre', 'N/A')
        print(f"\n  {status} Escenario {result['scenario_id']}: {result['nombre']}")
        print(f"      Lead: {nombre}")
        print(f"      Session: {result['session_id']}")
        if result['errores']:
            for error in result['errores'][:3]:
                print(f"      ⚠️  {error}")

    print(f"\n{'═' * 80}")
    print("Los contactos deberían estar ahora en HubSpot CRM.")
    print("Verifica en: https://app.hubspot.com/contacts/")
    print(f"{'═' * 80}")

    return all_results


async def run_single_scenario(
    base_url: str,
    scenario_id: int,
    verbose: bool = True
) -> Optional[Dict[str, Any]]:
    """Ejecuta un solo escenario específico."""

    scenarios = get_scenarios()
    scenario = next((s for s in scenarios if s['id'] == scenario_id), None)

    if not scenario:
        print(f"❌ Error: Escenario {scenario_id} no encontrado.")
        print(f"   Escenarios disponibles: 1-{len(scenarios)}")
        return None

    print_header(f"EJECUTANDO ESCENARIO {scenario_id}")
    print(f"API URL: {base_url}")

    async with ConversationClient(base_url) as client:
        if not await client.health_check():
            print("❌ ERROR: No se puede conectar con la API")
            return None

        return await run_scenario(client, scenario, verbose)


def list_scenarios():
    """Lista todos los escenarios disponibles."""
    scenarios = get_scenarios()

    print_header("ESCENARIOS DISPONIBLES")
    print(f"Total: {len(scenarios)} escenarios\n")

    for s in scenarios:
        print(f"{s['id']:2}. {s['nombre']}")
        print(f"    {s['descripcion']}")
        print(f"    Perfil: {s['perfil']['nombre']}, {s['perfil']['edad']} años")
        print(f"    Canal: {s['perfil']['canal_origen']}")
        print()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Script de pruebas de conversación para Sofía + HubSpot CRM"
    )
    parser.add_argument(
        "--scenario", "-s",
        type=int,
        help="Ejecutar solo un escenario específico (1-17). Escenarios 13-17 son de llegada por link."
    )
    parser.add_argument(
        "--local", "-l",
        action="store_true",
        help="Usar servidor local (localhost:8000) en lugar de producción"
    )
    parser.add_argument(
        "--url",
        type=str,
        help="URL personalizada de la API"
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Modo silencioso (menos output)"
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="No pausar entre escenarios"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Listar todos los escenarios disponibles"
    )

    args = parser.parse_args()

    if args.list:
        list_scenarios()
        return

    # Determinar URL de la API
    if args.url:
        base_url = args.url
    elif args.local:
        base_url = API_URL_LOCAL
    else:
        base_url = API_URL_PRODUCTION

    # Ejecutar escenarios
    if args.scenario:
        asyncio.run(run_single_scenario(
            base_url,
            args.scenario,
            verbose=not args.quiet
        ))
    else:
        asyncio.run(run_all_scenarios(
            base_url,
            verbose=not args.quiet,
            pause_between=not args.no_pause
        ))


if __name__ == "__main__":
    main()