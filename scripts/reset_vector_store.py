"""
Script de limpieza del Vector Store pgvector.
Elimina completamente la colección rag_knowledge_base y la recrea vacía.
"""

import os
import sys

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

# Obtener las variables de entorno
DATABASE_URL = os.getenv("DATABASE_URL")
VECTOR_COLLECTION_NAME = os.getenv("VECTOR_COLLECTION_NAME", "rag_knowledge_base")

print("\n" + "="*60)
print("🧨 LIMPIEZA TOTAL DEL VECTOR STORE")
print("="*60 + "\n")

print("📂 Configuración cargada:")
print(f"   DATABASE_URL: {DATABASE_URL[:50]}..." if DATABASE_URL else "   ❌ DATABASE_URL no configurada")
print(f"   VECTOR_COLLECTION_NAME: {VECTOR_COLLECTION_NAME}\n")

if not DATABASE_URL:
    print("❌ ERROR: DATABASE_URL no está configurada en .env")
    sys.exit(1)

try:
    print("🔌 Inicializando conexión a PostgreSQL + pgvector...")
    from rag.vector_store import pg_vector_store
    
    pg_vector_store.initialize_db()
    print("   ✅ Conexión exitosa\n")
    
    print("🔥 Eliminando colección del vector store...")
    pg_vector_store.delete_collection()
    print("   ✅ Colección eliminada\n")
    
    print("="*60)
    print("✅ LIMPIEZA COMPLETADA EXITOSAMENTE")
    print("="*60)
    print("\n🎯 Siguiente paso: Ejecutar la reindexación de knowledge base\n")
    
except (ValueError, ConnectionError, RuntimeError) as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
