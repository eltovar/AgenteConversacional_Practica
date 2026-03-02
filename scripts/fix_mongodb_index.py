#!/usr/bin/env python
"""
Script de migración para corregir el índice message_sid_unique_idx.

El índice anterior usaba sparse=True, que no funciona correctamente
con valores null explícitos. Este script:
1. Elimina el índice problemático
2. Lo recrea con partialFilterExpression que sí funciona con nulls

Ejecutar en producción:
    python scripts/fix_mongodb_index.py
"""

import asyncio
import os
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

# Cargar variables de entorno desde .env
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


async def fix_message_sid_index():
    """Corrige el índice message_sid_unique_idx."""
    # Priorizar MONGO_PUBLIC_URL para ejecución local (la URL interna de Railway no es accesible)
    mongo_url = os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGO_URL")
    
    if not mongo_url:
        print("❌ Error: MONGO_URL o MONGO_PUBLIC_URL no está configurada")
        print("   Configura la variable de entorno en .env o antes de ejecutar")
        return False
    
    # Detectar si estamos usando URL pública o interna
    is_public = "proxy.rlwy.net" in mongo_url or "shortline" in mongo_url
    print(f"🔧 Conectando a MongoDB ({'URL pública' if is_public else 'URL interna'})...")
    client = AsyncIOMotorClient(mongo_url)
    db = client.inmobiliaria_chat
    
    try:
        # 1. Ver índices actuales
        print("\n📋 Índices actuales en messages:")
        indexes = await db.messages.index_information()
        for name, info in indexes.items():
            print(f"   - {name}: unique={info.get('unique', False)}, sparse={info.get('sparse', False)}")
        
        # 2. Eliminar índice problemático si existe
        if "message_sid_unique_idx" in indexes:
            print("\n🗑️  Eliminando índice message_sid_unique_idx...")
            await db.messages.drop_index("message_sid_unique_idx")
            print("   ✅ Índice eliminado")
        else:
            print("\n⚠️  El índice message_sid_unique_idx no existe")
        
        # 3. Crear nuevo índice con partialFilterExpression
        print("\n🔨 Creando nuevo índice con partialFilterExpression...")
        await db.messages.create_index(
            "message_sid",
            name="message_sid_unique_idx",
            unique=True,
            partialFilterExpression={"message_sid": {"$type": "string"}}
        )
        print("   ✅ Nuevo índice creado correctamente")
        
        # 4. Verificar resultado
        print("\n📋 Índices después de la migración:")
        indexes = await db.messages.index_information()
        for name, info in indexes.items():
            if name == "message_sid_unique_idx":
                partial = info.get("partialFilterExpression", "ninguno")
                print(f"   ✅ {name}: unique={info.get('unique', False)}, partialFilter={partial}")
            else:
                print(f"   - {name}")
        
        print("\n🎉 Migración completada exitosamente!")
        return True
        
    except Exception as e:
        print(f"\n❌ Error durante la migración: {e}")
        return False
    finally:
        client.close()


if __name__ == "__main__":
    print("=" * 60)
    print("MIGRACIÓN: Corrección de índice message_sid_unique_idx")
    print("=" * 60)
    
    success = asyncio.run(fix_message_sid_index())
    exit(0 if success else 1)
