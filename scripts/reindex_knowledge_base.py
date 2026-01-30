"""
Script de reindexación de Knowledge Base.
Carga todos los archivos .txt de la carpeta knowledge_base y los indexa en pgvector.
"""

import os
import sys

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from logging_config import logger

# Cargar variables de entorno desde .env
load_dotenv()

print("\n" + "="*60)
print("📚 REINDEXACIÓN DE KNOWLEDGE BASE")
print("="*60 + "\n")

try:
    print("🔌 Inicializando servicios...")
    
    # Importar los servicios necesarios
    from rag.vector_store import pg_vector_store
    from rag.rag_service import rag_service
    
    # Inicializar la conexión a la DB
    print("   🔗 Inicializando conexión a pgvector...")
    pg_vector_store.initialize_db()
    print("   ✅ Conexión exitosa\n")
    
    # Recargar la knowledge base
    print("📂 Cargando documentos de knowledge_base/...")
    result = rag_service.reload_knowledge_base()
    
    print(f"\n✅ REINDEXACIÓN COMPLETADA:")
    print(f"   📄 Documentos cargados: {result.get('documents_loaded', 0)}")
    print(f"   ✂️  Chunks creados: {result.get('chunks_indexed', 0)}")
    print(f"   🆔 IDs indexados: {len(result.get('ids', []))}")
    
    print("\n" + "="*60)
    print("✅ KNOWLEDGE BASE LISTA")
    print("="*60)
    print("\n🎯 Siguiente paso: Iniciar la aplicación con 'python main.py'\n")
    
except Exception as e:
    print(f"\n❌ ERROR durante reindexación: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
