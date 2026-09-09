#!/usr/bin/env python3
"""
Script para verificar qué propiedades 'chatbot_*' existen en HubSpot.

Uso:
    python scripts/check_hubspot_properties.py
"""

import os
import sys

try:
    import httpx
except ImportError:
    print("❌ Instala httpx: pip install httpx")
    sys.exit(1)

# Cargar .env manualmente
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Si no está importado, leer .env manualmente
    env_file = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
BASE_URL = "https://api.hubapi.com"


def check_chatbot_properties() -> dict:
    """
    Verifica qué propiedades chatbot_* existen en HubSpot.
    """
    if not HUBSPOT_API_KEY:
        print("❌ ERROR: HUBSPOT_API_KEY no está configurada en .env")
        sys.exit(1)

    # Endpoint para listar propiedades de Contacts
    endpoint = f"{BASE_URL}/crm/v3/properties/contacts"

    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json"
    }

    print("=" * 70)
    print("VERIFICANDO PROPIEDADES 'chatbot_*' EN HUBSPOT")
    print("=" * 70)

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(endpoint, headers=headers)

            if response.status_code == 200:
                data = response.json()
                properties = data.get("results", [])
                
                # Filtrar propiedades que contienen 'chatbot'
                chatbot_properties = [p for p in properties if "chatbot" in p.get("name", "").lower()]
                
                if not chatbot_properties:
                    print("\n⚠️  NO HAY propiedades 'chatbot' configuradas en HubSpot\n")
                    print("   Propiedades esperadas que faltan:")
                    print("   - chatbot_conversation")
                    print("   - chatbot_timestamp")
                    print("   - chatbot_summary ❌ (la que está causando el error)")
                    print("   - chatbot_sentiment_alert")
                    print("   - chatbot_suspicious_indicators")
                    print("   - chatbot_social_media_link")
                    print("   - chatbot_canal_origen")
                    print("   - chatbot_social_media_url\n")
                    return {"status": "no_properties"}
                
                print(f"\n✅ Se encontraron {len(chatbot_properties)} propiedades 'chatbot':\n")
                
                # Mostrar propiedades base
                base_props = ["chatbot_conversation", "chatbot_timestamp"]
                print("📌 PROPIEDADES BASE (críticas):")
                for prop in base_props:
                    found = any(p["name"] == prop for p in chatbot_properties)
                    status = "✅ EXISTE" if found else "❌ FALTA"
                    print(f"   {prop:<30} {status}")
                
                print()
                
                # Mostrar propiedad problemática
                summary_exists = any(p["name"] == "chatbot_summary" for p in chatbot_properties)
                print("⚠️  PROPIEDAD PROBLEMÁTICA (causa del error):")
                if summary_exists:
                    status = "✅ EXISTE"
                else:
                    status = "❌ FALTA (← ERROR actual)"
                print(f"   chatbot_summary{' ' * 18} {status}\n")
                
                # Mostrar propiedades opcionales
                optional_props = [
                    "chatbot_sentiment_alert",
                    "chatbot_suspicious_indicators",
                    "chatbot_social_media_link",
                    "chatbot_canal_origen",
                    "chatbot_social_media_url"
                ]
                print("✨ PROPIEDADES OPCIONALES:")
                for prop in optional_props:
                    found = any(p["name"] == prop for p in chatbot_properties)
                    status = "✅ EXISTE" if found else "⏭️  OPCIONAL"
                    print(f"   {prop:<30} {status}")
                
                print("\n" + "=" * 70)
                print("RESUMEN")
                print("=" * 70)
                
                if summary_exists:
                    print("\n✅ chatbot_summary EXISTE en HubSpot")
                    print("   → El error puede ser por otro motivo")
                    print("   → Revisa si la propiedad está oculta o tiene restricciones\n")
                else:
                    print("\n❌ chatbot_summary NO EXISTE en HubSpot")
                    print("   → Por eso falla el webhook con 400 PROPERTY_DOESNT_EXIST")
                    print("   → Opciones para arreglarlo:")
                    print("      1. Crear la propiedad en HubSpot")
                    print("      2. Remover 'chatbot_summary' del código")
                    print("      3. Mover a propiedades opcionales\n")
                
                return {
                    "status": "found",
                    "total": len(chatbot_properties),
                    "summary_exists": summary_exists,
                    "properties": [p["name"] for p in chatbot_properties]
                }

            elif response.status_code == 401:
                print("\n❌ ERROR 401: Token inválido o expirado")
                print(f"   HUBSPOT_API_KEY: {HUBSPOT_API_KEY[:30]}...")
                return {"error": "invalid_token"}

            elif response.status_code == 403:
                print("\n❌ ERROR 403: Permiso denegado")
                print("   Tu API Key no tiene acceso a Properties")
                return {"error": "permission_denied"}

            else:
                print(f"\n❌ ERROR {response.status_code}")
                print(f"   Respuesta: {response.text}\n")
                return {"error": response.text}

    except Exception as e:
        print(f"\n❌ Error de conexión: {e}\n")
        return {"error": str(e)}


if __name__ == "__main__":
    check_chatbot_properties()
