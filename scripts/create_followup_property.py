# scripts/create_followup_property.py
"""
Script para crear la propiedad 'last_followup_date' en HubSpot.

Esta propiedad se usa para:
1. Registrar cuándo se envió el último follow-up a un contacto
2. Evitar enviar follow-ups duplicados (ventana de 7 días)

Ejecutar una vez antes de usar el sistema de follow-ups:
    python scripts/create_followup_property.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Cargar variables de entorno
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

ACCESS_TOKEN = os.getenv("HUBSPOT_API_KEY")


def create_last_followup_date_property():
    """
    Crea la propiedad 'last_followup_date' en HubSpot para contactos.
    """
    if not ACCESS_TOKEN:
        print("❌ ERROR: HUBSPOT_API_KEY no configurada en .env")
        return

    url = "https://api.hubapi.com/crm/v3/properties/contacts"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "name": "last_followup_date",
        "label": "Fecha Último Follow-up",
        "description": "Fecha del último mensaje de seguimiento enviado por el sistema automatizado",
        "groupName": "contactinformation",
        "type": "date",
        "fieldType": "date"
    }

    print("🚀 Creando propiedad 'last_followup_date' en HubSpot...")

    try:
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            print("\n✅ ¡PROPIEDAD CREADA CON ÉXITO!")
            print("-" * 50)
            print("Nombre interno: last_followup_date")
            print("Tipo: Fecha")
            print("-" * 50)
            print("\nAhora puedes usar el script de follow-up:")
            print("  python scripts/follow_up_scheduler.py --dry-run")

        elif response.status_code == 409:
            print("\n⚠️ La propiedad 'last_followup_date' ya existe.")
            print("Puedes usarla directamente.")

        else:
            print(f"\n❌ Error {response.status_code}: {response.text}")

    except Exception as e:
        print(f"\n❌ Error de conexión: {str(e)}")


def create_comunicaciones_whatsapp_property():
    """
    Crea la propiedad 'comunicaciones_whatsapp' para opt-out.
    """
    if not ACCESS_TOKEN:
        print("❌ ERROR: HUBSPOT_API_KEY no configurada en .env")
        return

    url = "https://api.hubapi.com/crm/v3/properties/contacts"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "name": "comunicaciones_whatsapp",
        "label": "Comunicaciones WhatsApp",
        "description": "Indica si el contacto acepta recibir comunicaciones por WhatsApp",
        "groupName": "contactinformation",
        "type": "enumeration",
        "fieldType": "booleancheckbox",
        "options": [
            {
                "label": "Sí",
                "value": "true",
                "displayOrder": 0
            },
            {
                "label": "No",
                "value": "false",
                "displayOrder": 1
            }
        ]
    }

    print("🚀 Creando propiedad 'comunicaciones_whatsapp' en HubSpot...")

    try:
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            print("\n✅ ¡PROPIEDAD CREADA CON ÉXITO!")
            print("-" * 50)
            print("Nombre interno: comunicaciones_whatsapp")
            print("Tipo: Checkbox (Sí/No)")
            print("Default: Sí (acepta comunicaciones)")
            print("-" * 50)

        elif response.status_code == 409:
            print("\n⚠️ La propiedad 'comunicaciones_whatsapp' ya existe.")

        else:
            print(f"\n❌ Error {response.status_code}: {response.text}")

    except Exception as e:
        print(f"\n❌ Error de conexión: {str(e)}")


if __name__ == "__main__":
    print("=" * 60)
    print("Configuración de Propiedades para Follow-up")
    print("=" * 60)
    print()

    # Crear ambas propiedades
    create_last_followup_date_property()
    print()
    create_comunicaciones_whatsapp_property()

    print()
    print("=" * 60)
    print("Configuración completada")
    print("=" * 60)