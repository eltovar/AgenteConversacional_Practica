import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Asegurar que el path del proyecto esté disponible
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Intentamos importar la clase para extraer la lógica real
try:
    from integrations.hubspot.timeline_logger import TimelineLogger
except ImportError:
    TimelineLogger = MagicMock()

@pytest.fixture
def mock_logger():
    """
    Crea un mock de TimelineLogger que inyecta la lógica real de los métodos 
    pero mantiene las dependencias (Redis) simuladas.
    """
    # Creamos el mock con la especificación de la clase
    logger = MagicMock(spec=TimelineLogger)
    
    # Configuramos el cliente de redis mockeado como un AsyncMock
    # Importante: AsyncMock es necesario para métodos awaitables como delete
    mock_redis = AsyncMock()
    mock_redis.delete = AsyncMock(return_value=1)
    
    # Mapeamos todos los nombres posibles que tu código podría usar internamente
    # Esto asegura que 'self.redis', 'self.redis_client', etc., apunten al mismo mock
    logger.redis = mock_redis
    logger.redis_client = mock_redis
    logger._redis = mock_redis
    logger.client = mock_redis
    
    # Mock el método _get_redis para que retorne el mock de Redis
    logger._get_redis = AsyncMock(return_value=mock_redis)
    
    # Inyectamos la lógica real de los métodos para testear el algoritmo real
    try:
        from integrations.hubspot.timeline_logger import TimelineLogger as RealLogger
        
        # Inyectamos los métodos directamente. Al usarlos, pasaremos 'logger' como primer argumento (self)
        logger._detect_sender_from_body = RealLogger._detect_sender_from_body
        logger.invalidate_cache_for_contact = RealLogger.invalidate_cache_for_contact
    except Exception as e:
        print(f"Advertencia: No se pudo cargar la lógica real: {e}")
        
    return logger

@pytest.mark.parametrize("body,expected_id", [
    ("📱 Cliente: Hola, necesito ayuda", "client"),
    ("🤖 Sofía: Hola, soy tu asistente", "bot"),
    ("👤 [Asesor] ➡️\n\nClaro, te ayudo", "advisor"),
    ("Nota manual: El cliente llamó por teléfono", "manual_note"),
    ("Cualquier otro texto", "manual_note"),
])
def test_detect_sender_from_body(mock_logger, body, expected_id):
    """
    Valida la lógica de detección comparando el primer elemento de la tupla 
    devuelta (el ID del sender).
    """
    # Ejecutamos la lógica real inyectada pasando el mock como self
    result = mock_logger._detect_sender_from_body(mock_logger, body)
    
    assert isinstance(result, tuple), f"Se esperaba una tupla, se obtuvo {type(result)}"
    assert result[0] == expected_id, f"Error detectando ID para: {body}. Obtuve: {result[0]}"

@pytest.mark.asyncio
async def test_invalidate_cache_for_contact(mock_logger):
    """
    Verifica que la función de invalidación de caché llame a Redis correctamente.
    """
    contact_id = "test_123"
    
    # Ejecutamos el método real inyectado pasando el mock como 'self'
    # Esto funciona porque se inyectó el método sin vincular de RealLogger
    await mock_logger.invalidate_cache_for_contact(mock_logger, contact_id)
    
    # Verificamos si se llamó a delete en el cliente redis inyectado.
    # Usamos mock_logger.redis que es nuestra referencia al AsyncMock de Redis.
    assert mock_logger._get_redis.called, (
        "El método _get_redis no fue detectado. "
        "Verify that the fixture has _get_redis mocked as AsyncMock."
    )
    
    # Verificar que se llamó a delete
    assert mock_logger.redis.delete.called, (
        "El método redis.delete no fue detectado. "
        "El método debe llamar a self._get_redis().delete() para invalidar el caché."
    )
    
    # Extraemos los argumentos de la llamada para validar la key
    args, _ = mock_logger.redis.delete.call_args
    cache_key = f"hs_assoc:contact:{contact_id}:notes"
    assert any(contact_id in str(arg) for arg in args), \
        f"El ID {contact_id} no se encontró en los argumentos de delete. Args: {args}"

@pytest.mark.parametrize("body,expected_display_name", [
    ("🤖 Sofía: Hola", "Sofía"),
    ("👤 [Asesor] ➡️\n\nJuan aquí", "Asesor"),
    ("📱 Cliente: Consulta", "Cliente"),
])
def test_detect_display_names(mock_logger, body, expected_display_name):
    """
    Verifica que el nombre a mostrar sea el correcto.
    """
    result = mock_logger._detect_sender_from_body(mock_logger, body)
    assert result[1] == expected_display_name, f"Nombre incorrecto para: {body}"

@pytest.mark.parametrize("body,expected_alignment", [
    ("🤖 Sofía: Mensaje", "right"),
    ("📱 Cliente: Mensaje", "left"),
    ("👤 [Asesor] ➡️\n\nAsesor", "right"),
])
def test_detect_alignment(mock_logger, body, expected_alignment):
    """
    Verifica que la alineación de la burbuja sea la correcta.
    """
    result = mock_logger._detect_sender_from_body(mock_logger, body)
    assert result[2] == expected_alignment, f"Alineación incorrecta para: {body}"