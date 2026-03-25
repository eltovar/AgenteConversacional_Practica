"""
Test: Verifica el número exacto de docs en pgvector.
Después de un startup correcto debe haber exactamente 42 chunks.

Uso: python scripts/test_pgvector_count.py
"""
import os
import psycopg
from dotenv import load_dotenv

load_dotenv()


def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ No se encontró DATABASE_URL en .env")
        return

    conn_str = db_url.replace("postgres://", "postgresql://")

    try:
        conn = psycopg.connect(conn_str)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM langchain_pg_embedding
                WHERE collection_id IN (
                    SELECT uuid FROM langchain_pg_collection WHERE name = %s
                )
                """,
                ("rag_knowledge_base",),
            )
            count = cur.fetchone()[0]
        conn.close()

        print(f"Docs en pgvector: {count}")
        if count == 42:
            print("[OK] exactamente 42 chunks (sin duplicados)")
        elif count == 0:
            print("[WARN] Vector store vacio - el startup no indexo nada")
        elif count % 42 == 0:
            duplicados = count // 42
            print(f"[FALLO] DUPLICADOS - hay {duplicados}x copias ({count} docs). Stampede detectado.")
        else:
            print(f"[FALLO] INESPERADO - {count} docs (esperado multiplo de 42)")

    except Exception as e:
        print(f"❌ Error conectando a PostgreSQL: {e}")


if __name__ == "__main__":
    main()
