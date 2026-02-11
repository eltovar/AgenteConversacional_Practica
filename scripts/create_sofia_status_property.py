#!/usr/bin/env python3
"""
Script para crear la propiedad 'sofia_status' en HubSpot.

Esta propiedad permite a los asesores ver y controlar el estado de Sofía
directamente desde la ficha del contacto.

Valores:
- activa: Sofía responde automáticamente
- pausada: Sofía no responde (un asesor está atendiendo)

Uso:
    python scripts/create_sofia_status_property.py
"""

import os
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
BASE_URL = "https://api.hubapi.com"


def create_sofia_status_property() -> dict:
    """
    Crea la propiedad 'sofia_status' en el objeto Contact de HubSpot.
    """
    if not HUBSPOT_API_KEY:
        print("ERROR: HUBSPOT_API_KEY no está configurada")
        sys.exit(1)

    endpoint = f"{BASE_URL}/crm/v3/properties/contacts"

    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }

    # Definición de la propiedad
    payload = {
        "name": "sofia_status",
        "label": "Estado de Sofía",
        "description": "Indica si Sofía (IA) está activa o pausada para este contacto. Si está pausada, un asesor humano está atendiendo.",
        "groupName": "contactinformation",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {
                "label": "Activa",
                "value": "activa",
                "description": "Sofía responde automáticamente",
                "displayOrder": 0
            },
            {
                "label": "Pausada",
                "value": "pausada",
                "description": "Un asesor está atendiendo manualmente",
                "displayOrder": 1
            }
        ],
        "formField": True,  # Visible en formularios
        "hasUniqueValue": False
    }

    print("=" * 60)
    print("CREANDO PROPIEDAD 'sofia_status' EN HUBSPOT")
    print("=" * 60)

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(endpoint, headers=headers, json=payload)

            if response.status_code == 201:
                data = response.json()
                print("\n✅ PROPIEDAD CREADA EXITOSAMENTE")
                print(f"   Nombre: {data.get('name')}")
                print(f"   Label: {data.get('label')}")
                print(f"   Tipo: {data.get('type')}")
                return data

            elif response.status_code == 409:
                print("\n⚠️  La propiedad 'sofia_status' ya existe.")
                print("   No es necesario crearla nuevamente.")
                return {"status": "already_exists"}

            else:
                print(f"\n❌ Error: {response.status_code}")
                print(f"   Respuesta: {response.text}")
                return {"error": response.text}

    except Exception as e:
        print(f"\n❌ Error de conexión: {e}")
        return {"error": str(e)}


def create_sofia_status_updated_property() -> dict:
    """
    Crea la propiedad auxiliar 'sofia_status_updated' para tracking.
    """
    if not HUBSPOT_API_KEY:
        return {"error": "No API key"}

    endpoint = f"{BASE_URL}/crm/v3/properties/contacts"

    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "name": "sofia_status_updated",
        "label": "Última actualización de Sofía",
        "description": "Fecha y hora de la última vez que se cambió el estado de Sofía",
        "groupName": "contactinformation",
        "type": "datetime",
        "fieldType": "date",
        "formField": False,
        "hasUniqueValue": False
    }

    print("\n" + "-" * 60)
    print("Creando propiedad auxiliar 'sofia_status_updated'...")

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(endpoint, headers=headers, json=payload)

            if response.status_code == 201:
                print("✅ Propiedad auxiliar creada")
                return response.json()

            elif response.status_code == 409:
                print("⚠️  Propiedad auxiliar ya existe")
                return {"status": "already_exists"}

            else:
                print(f"❌ Error: {response.status_code}")
                return {"error": response.text}

    except Exception as e:
        print(f"❌ Error: {e}")
        return {"error": str(e)}


def verify_properties_exist() -> bool:
    """
    Verifica que las propiedades necesarias existen en HubSpot.
    """
    if not HUBSPOT_API_KEY:
        return False

    endpoint = f"{BASE_URL}/crm/v3/properties/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}

    required_properties = ["sofia_status", "whatsapp_id"]

    print("\n" + "-" * 60)
    print("Verificando propiedades existentes...")

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(endpoint, headers=headers)

            if response.status_code == 200:
                data = response.json()
                existing = {p["name"] for p in data.get("results", [])}

                all_exist = True
                for prop in required_properties:
                    if prop in existing:
                        print(f"  ✅ {prop}")
                    else:
                        print(f"  ❌ {prop} (faltante)")
                        all_exist = False

                return all_exist

    except Exception as e:
        print(f"Error verificando propiedades: {e}")

    return False


if __name__ == "__main__":
    print("\n🔍 Verificando configuración actual...\n")

    # Verificar propiedades existentes
    verify_properties_exist()

    # Crear propiedad principal
    create_sofia_status_property()

    # Crear propiedad auxiliar
    create_sofia_status_updated_property()

    print("\n" + "=" * 60)
    print("CONFIGURACIÓN COMPLETADA")
    print("=" * 60)
    print("""
Ahora los asesores verán en la ficha del contacto:
- Campo "Estado de Sofía" con valores: Activa / Pausada
- Cuando respondan desde HubSpot, Sofía se pausará automáticamente
- Para reactivar a Sofía, cambiar el valor a "Activa" manualmente

Para configurar la reactivación automática, crea un Workflow en HubSpot:
1. Trigger: Si "Estado de Sofía" = "Pausada" Y última actividad > 30 min
2. Acción: Cambiar "Estado de Sofía" a "Activa"
""")