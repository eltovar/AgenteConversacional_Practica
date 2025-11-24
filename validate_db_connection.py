# validate_db_connection.py
"""
Script temporal para validar la conexión a PostgreSQL con pgvector.
Este script se puede ejecutar directamente para verificar que:
1. Las credenciales de PostgreSQL están correctamente configuradas
2. La extensión pgvector está disponible
3. Se pueden crear y consultar tablas de vectores
"""

import os
from dotenv import load_dotenv
from rag.vector_store import pg_vector_store
from langchain_core.documents import Document

# Cargar variables de entorno
load_dotenv()


def main():
    print("=" * 60)
    print("VALIDACIÓN DE CONEXIÓN A POSTGRESQL + PGVECTOR")
    print("=" * 60)

    # 1. Verificar variables de entorno
    print("\n[1/5] Verificando variables de entorno...")
    database_url = os.getenv("DATABASE_URL")
    openai_key = os.getenv("OPENAI_API_KEY")

    if not database_url:
        print("❌ ERROR: DATABASE_URL no está configurada")
        return False

    if not openai_key:
        print("❌ ERROR: OPENAI_API_KEY no está configurada")
        return False

    print(f"✓ DATABASE_URL configurada: {database_url[:30]}...")
    print(f"✓ OPENAI_API_KEY configurada: {openai_key[:20]}...")

    # 2. Inicializar conexión
    print("\n[2/5] Inicializando conexión a PostgreSQL...")
    try:
        result = pg_vector_store.initialize_db()
        if result:
            print("✓ Conexión a PostgreSQL establecida exitosamente")
            print(f"✓ Tabla '{pg_vector_store.collection_name}' creada/verificada")
        else:
            print("❌ Error al inicializar la base de datos")
            return False
    except Exception as e:
        print(f"❌ ERROR al conectar: {e}")
        return False

    # 3. Probar inserción de documentos
    print("\n[3/5] Probando inserción de documentos...")
    try:
        test_docs = [
            Document(
                page_content="Inmobiliaria Proteger ofrece apartamentos en Medellín.",
                metadata={"source": "test", "type": "validation"}
            ),
            Document(
                page_content="Tenemos casas disponibles en el Poblado con precios competitivos.",
                metadata={"source": "test", "type": "validation"}
            ),
            Document(
                page_content="Nuestros asesores están disponibles para ayudarte a encontrar tu hogar ideal.",
                metadata={"source": "test", "type": "validation"}
            )
        ]

        ids = pg_vector_store.add_documents(test_docs)
        print(f"✓ {len(ids)} documentos insertados exitosamente")
        print(f"  IDs: {ids[:2]}...")  # Mostrar primeros 2 IDs

    except Exception as e:
        print(f"❌ ERROR al insertar documentos: {e}")
        return False

    # 4. Probar búsqueda semántica
    print("\n[4/5] Probando búsqueda semántica...")
    try:
        query = "Quiero comprar un apartamento"
        results = pg_vector_store.similarity_search(query, k=2)

        print(f"✓ Búsqueda completada: {len(results)} resultados encontrados")
        for i, doc in enumerate(results, 1):
            print(f"\n  Resultado {i}:")
            print(f"    Contenido: {doc.page_content[:80]}...")
            print(f"    Metadata: {doc.metadata}")

    except Exception as e:
        print(f"❌ ERROR en búsqueda semántica: {e}")
        return False

    # 5. Verificar embeddings
    print("\n[5/5] Verificando generación de embeddings...")
    try:
        from llm_client import embeddings
        test_text = "Texto de prueba para embeddings"
        embedding_vector = embeddings.embed_query(test_text)

        print(f"✓ Embedding generado exitosamente")
        print(f"  Dimensiones: {len(embedding_vector)}")
        print(f"  Primeros valores: {embedding_vector[:5]}")

    except Exception as e:
        print(f"❌ ERROR al generar embeddings: {e}")
        return False

    # Resumen final
    print("\n" + "=" * 60)
    print("✅ VALIDACIÓN COMPLETADA EXITOSAMENTE")
    print("=" * 60)
    print("\nTodos los componentes están funcionando correctamente:")
    print("  ✓ Conexión a PostgreSQL")
    print("  ✓ Extensión pgvector")
    print("  ✓ Creación de tablas")
    print("  ✓ Inserción de documentos")
    print("  ✓ Búsqueda semántica")
    print("  ✓ Generación de embeddings")
    print("\n¡El sistema RAG con pgvector está listo para usar!")

    return True


if __name__ == "__main__":
    try:
        success = main()
        exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Validación interrumpida por el usuario")
        exit(1)
    except Exception as e:
        print(f"\n\n❌ ERROR INESPERADO: {e}")
        import traceback
        traceback.print_exc()
        exit(1)