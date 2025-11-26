"""
Script de inicialización con retry para Railway.
Espera a que Redis y PostgreSQL estén disponibles antes de iniciar la app.
"""
import os
import time
import sys
import redis
import psycopg
from psycopg import Connection
from logging_config import logger

def wait_for_redis(max_retries=30, delay=2):
    """Espera a que Redis esté disponible."""
    redis_url = os.getenv("REDIS_URL")
    
    if not redis_url:
        logger.warning("REDIS_URL no configurada, saltando verificación")
        return True
    
    for attempt in range(max_retries):
        try:
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()
            client.close()
            logger.info(f"✓ Redis conectado en intento {attempt + 1}")
            return True
        except Exception as e:
            logger.warning(f"Redis no disponible (intento {attempt + 1}/{max_retries}): {e}")
            time.sleep(delay)
    
    logger.error("Redis no disponible después de múltiples intentos")
    return False

def wait_for_postgres(max_retries=30, delay=2) -> bool:
    """Espera a que PostgreSQL esté disponible."""
    database_url = os.getenv("DATABASE_URL")
    
    if not database_url:
        logger.warning("DATABASE_URL no configurada, saltando verificación")
        return True
    
    for attempt in range(max_retries):
        try:
            with psycopg.connect(database_url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
            logger.info(f"✓ PostgreSQL conectado en intento {attempt + 1}")
            return True
        except Exception as e:
            logger.warning(f"PostgreSQL no disponible (intento {attempt + 1}/{max_retries}): {e}")
            time.sleep(delay)
    
    logger.error("PostgreSQL no disponible después de múltiples intentos")
    return False

def main():
    """Espera a que las dependencias estén listas y luego inicia la app."""
    logger.info("🚀 Iniciando verificación de dependencias...")
    
    # Verificar Redis
    if not wait_for_redis():
        logger.error("❌ No se pudo conectar a Redis")
        sys.exit(1)
    
    # Verificar PostgreSQL
    if not wait_for_postgres():
        logger.error("❌ No se pudo conectar a PostgreSQL")
        sys.exit(1)
    
    logger.info("✅ Todas las dependencias están listas")
    
    # Importar y ejecutar la app
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    
    logger.info(f"🚀 Iniciando servidor en puerto {port}")
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        log_level="info"
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n⚠️ Sistema detenido por usuario (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        logger.error(f"\n❌ ERROR CRÍTICO EN STARTUP: {e}", exc_info=True)
        sys.exit(1)