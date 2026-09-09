#!/usr/bin/env python3
"""
Script de prueba para validar que la subida a Bunny.net funciona correctamente.
Prueba la carga de un archivo pequeño de prueba.
"""

import os
import sys
import httpx
import asyncio
from datetime import datetime

# Cargar variables del .env
def load_env_file(filepath=".env"):
    """Carga variables del archivo .env"""
    if os.path.exists(filepath):
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value

load_env_file()

# Configuración
BUNNY_STORAGE_ZONE = os.getenv("BUNNY_STORAGE_ZONE_NAME")
BUNNY_API_KEY = os.getenv("BUNNY_STORAGE_API_KEY")
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE_URL", "").rstrip('/')
BUNNY_ENDPOINT = os.getenv("BUNNY_STORAGE_ENDPOINT", "ny.storage.bunnycdn.com")

print("=" * 80)
print("PRUEBA DE SUBIDA A BUNNY STORAGE")
print("=" * 80)

print(f"\n📋 CONFIGURACIÓN:")
print(f"  Zone: {BUNNY_STORAGE_ZONE}")
print(f"  Endpoint: {BUNNY_ENDPOINT}")
print(f"  Pull Zone: {BUNNY_PULL_ZONE}")
print(f"  API Key: {BUNNY_API_KEY[:10]}...{BUNNY_API_KEY[-10:] if BUNNY_API_KEY else 'NO CONFIGURADA'}")

if not all([BUNNY_STORAGE_ZONE, BUNNY_API_KEY, BUNNY_ENDPOINT]):
    print("\n❌ ERROR: Faltan variables de configuración en .env")
    sys.exit(1)


async def test_bunny_upload():
    """Intenta subir un archivo pequeño a Bunny"""
    
    # Crear contenido de prueba
    test_content = f"Prueba de conexión Sofia - {datetime.now().isoformat()}".encode()
    filename = "test_sofia_connection.txt"
    folder = "test"
    
    # Construir URL
    storage_url = f"https://{BUNNY_ENDPOINT}/{BUNNY_STORAGE_ZONE}/{folder}/{filename}"
    
    # Preparar headers
    headers = {
        "AccessKey": BUNNY_API_KEY,
        "Content-Type": "application/octet-stream",
        "accept": "application/json"
    }
    
    print(f"\n📤 INTENTANDO SUBIDA:")
    print(f"  URL: {storage_url}")
    print(f"  Tamaño: {len(test_content)} bytes")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(storage_url, content=test_content, headers=headers)
            
            print(f"\n📊 RESPUESTA:")
            print(f"  Status Code: {response.status_code}")
            print(f"  Response: {response.text}")
            
            if response.status_code in [200, 201]:
                print(f"\n✅ ¡ÉXITO! Archivo subido correctamente")
                
                # Construir URL pública
                public_url = f"{BUNNY_PULL_ZONE}/{folder}/{filename}"
                print(f"\n🌐 URL PÚBLICA DEL CDN:")
                print(f"  {public_url}")
                
                print(f"\n💡 Los archivos ahora se subirán correctamente a Bunny.net")
                return True
            else:
                print(f"\n❌ ERROR en Bunny Storage")
                return False
                
    except httpx.TimeoutException:
        print(f"\n❌ ERROR: Timeout conectando a Bunny Storage")
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return False


if __name__ == "__main__":
    print("\n⏳ Ejecutando prueba...\n")
    success = asyncio.run(test_bunny_upload())
    
    print("\n" + "=" * 80)
    if success:
        print("✅ TODO LISTO - Puedes ejecutar tus tests de Postman")
        sys.exit(0)
    else:
        print("❌ PROBLEMA DETECTADO - Revisa la configuración")
        sys.exit(1)
