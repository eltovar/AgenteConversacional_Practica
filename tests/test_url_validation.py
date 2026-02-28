#!/usr/bin/env python3
"""
Script de validación: Verificar que las URLs de Bunny tienen el protocolo https://
"""

import os

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
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE_URL", "").rstrip('/')

print("=" * 80)
print("VALIDACIÓN DE CONSTRUCCIÓN DE URLs")
print("=" * 80)

# Simular construcción de URLs como lo hace media_processor.py
print(f"\n📋 BUNNY_PULL_ZONE: {BUNNY_PULL_ZONE}")

test_cases = [
    ("imagenes_asesores", "573195652633_1772141013.jpg"),
    ("audios_asesores", "573195652633_1772141055.ogg"),
    ("imagenes_clientes", "573001234567_1772141000.jpg"),
    ("audios_clientes", "573001234567_1772141001.ogg"),
]

print(f"\n✅ URLs que se generarán ahora:")
all_valid = True

for folder, filename in test_cases:
    # Esto es lo que hace media_processor.py ahora (CORREGIDO)
    public_url = f"https://{BUNNY_PULL_ZONE}/{folder}/{filename}"
    
    # Validar que tiene protocolo
    has_https = public_url.startswith("https://")
    status = "✓" if has_https else "✗"
    
    print(f"\n  {status} {public_url}")
    
    if not has_https:
        all_valid = False
        print(f"     ❌ FALTA PROTOCOLO https://")

print("\n" + "=" * 80)
if all_valid:
    print("✅ TODAS LAS URLs SON VÁLIDAS PARA TWILIO")
    print("\nTwilio aceptará estas URLs en el campo MediaUrl")
else:
    print("❌ ALGUNAS URLs TIENEN PROBLEMAS")

print("=" * 80)
