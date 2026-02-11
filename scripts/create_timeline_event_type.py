#!/usr/bin/env python3
"""
Este script registra un tipo de evento personalizado que permite
visualizar los mensajes de Sofía en el Timeline de los contactos.

Uso:
    python scripts/create_timeline_event_type.py
    
El script guardará el EVENT_TYPE_ID resultante para usar en el código.
"""

import os
import sys
import httpx
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
BASE_URL = "https://api.hubapi.com"


def create_timeline_event_type() -> dict:
    """
    Crea el Event Type para mensajes de WhatsApp en HubSpot Timeline.

    Returns:
        dict con la respuesta de HubSpot incluyendo el 'id' del event type
    """
    if not HUBSPOT_API_KEY:
        print("ERROR: HUBSPOT_API_KEY no está configurada en .env")
        sys.exit(1)

    endpoint = f"{BASE_URL}/crm/v3/timeline/events/templates"

    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }

    # Definición del Event Type
    payload = {
        "name": "Mensaje WhatsApp Sofía",
        "objectType": "CONTACT",
        "headerTemplate": "{{#if es_bot}}🤖 Sofía (IA){{else}}👤 Cliente{{/if}}: {{direccion_label}}",
        "detailTemplate": """**{{emisor}}**

{{contenido}}

---
_Canal: WhatsApp | {{timestamp}}_""",
        "tokens": [
            {
                "name": "contenido",
                "label": "Contenido del mensaje",
                "type": "string"
            },
            {
                "name": "emisor",
                "label": "Emisor del mensaje",
                "type": "string"
            },
            {
                "name": "es_bot",
                "label": "Es mensaje del bot",
                "type": "boolean"
            },
            {
                "name": "timestamp",
                "label": "Fecha y hora",
                "type": "string"
            },
            {
                "name": "direccion",
                "label": "Dirección",
                "type": "enumeration",
                "options": [
                    {"value": "inbound", "label": "Entrante"},
                    {"value": "outbound", "label": "Saliente"}
                ]
            },
            {
                "name": "direccion_label",
                "label": "Etiqueta de dirección",
                "type": "string"
            },
            {
                "name": "session_id",
                "label": "ID de sesión WhatsApp",
                "type": "string"
            }
        ]
    }

    print("=" * 60)
    print("CREANDO EVENT TYPE EN HUBSPOT TIMELINE")
    print("=" * 60)
    print(f"\nEndpoint: {endpoint}")
    print(f"Nombre del evento: {payload['name']}")
    print(f"Tokens definidos: {len(payload['tokens'])}")

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(endpoint, headers=headers, json=payload)

            if response.status_code == 201:
                data = response.json()
                event_type_id = data.get("id")

                print("\n" + "=" * 60)
                print("✅ EVENT TYPE CREADO EXITOSAMENTE")
                print("=" * 60)
                print(f"\n📋 Event Type ID: {event_type_id}")
                print(f"   Nombre: {data.get('name')}")
                print(f"   Object Type: {data.get('objectType')}")

                print("\n" + "-" * 60)
                print("SIGUIENTE PASO:")
                print("-" * 60)
                print(f"\nAgrega esta línea a tu archivo .env:\n")
                print(f"   HUBSPOT_TIMELINE_EVENT_TYPE_ID={event_type_id}")
                print("\n" + "=" * 60)

                return data

            elif response.status_code == 409:
                print("\n⚠️  El Event Type ya existe.")
                print("   Si necesitas el ID, búscalo en HubSpot Settings > Integrations")
                data = response.json()
                print(f"   Detalle: {data}")
                return data

            else:
                print(f"\n❌ Error: {response.status_code}")
                print(f"   Respuesta: {response.text}")
                return {"error": response.text, "status_code": response.status_code}

    except httpx.RequestError as e:
        print(f"\n❌ Error de conexión: {e}")
        return {"error": str(e)}


def list_existing_event_types() -> list:
    """
    Lista los Event Types existentes para verificar si ya existe uno.
    """
    if not HUBSPOT_API_KEY:
        return []

    endpoint = f"{BASE_URL}/crm/v3/timeline/events/templates"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(endpoint, headers=headers)

            if response.status_code == 200:
                data = response.json()
                return data.get("results", [])
            else:
                print(f"Error listando event types: {response.status_code}")
                return []
    except Exception as e:
        print(f"Error: {e}")
        return []


if __name__ == "__main__":
    print("\n🔍 Verificando Event Types existentes...\n")

    existing = list_existing_event_types()

    if existing:
        print(f"Event Types encontrados: {len(existing)}")
        for et in existing:
            print(f"  - ID: {et.get('id')} | Nombre: {et.get('name')}")

        # Buscar si ya existe uno de Sofía/WhatsApp
        sofia_event = next(
            (et for et in existing if "sofia" in et.get("name", "").lower() or "whatsapp" in et.get("name", "").lower()),
            None
        )

        if sofia_event:
            print(f"\n✅ Ya existe un Event Type para Sofía/WhatsApp:")
            print(f"   ID: {sofia_event.get('id')}")
            print(f"   Nombre: {sofia_event.get('name')}")
            print(f"\n   Agrega a tu .env: HUBSPOT_TIMELINE_EVENT_TYPE_ID={sofia_event.get('id')}")
            sys.exit(0)

    print("\n📝 No se encontró Event Type existente. Creando uno nuevo...\n")
    create_timeline_event_type()