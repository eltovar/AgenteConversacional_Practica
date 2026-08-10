"""
Configuracion compartida de la suite.

Los modulos del middleware validan credenciales al importarse (por ejemplo
hubspot_client lanza ValueError si falta HUBSPOT_API_KEY). Sin esto, cualquier
test que importe middleware.outbound_panel falla en maquinas sin .env — y peor,
PASA en la maquina del que tiene .env, dando una suite que solo funciona en un
sitio.

Se ponen valores dummy y solo si la variable NO existe ya, de modo que un
entorno real no se pisa.

⚠️ Ninguna credencial real vive aqui. Estos valores no autentican contra nada;
los tests de la suite son de analisis estatico o con dobles de prueba, no
hacen llamadas de red.
"""
import os

_DUMMY_ENV = {
    "HUBSPOT_API_KEY": "test-dummy-not-a-real-key",
    "HUBSPOT_ACCESS_TOKEN": "test-dummy-not-a-real-token",
    "OPENAI_API_KEY": "test-dummy-not-a-real-key",
    "TWILIO_ACCOUNT_SID": "ACtestdummy00000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test-dummy-not-a-real-token",
    "TWILIO_PHONE_NUMBER": "+10000000000",
    "ADMIN_API_KEY": "test-dummy-admin-key",
    # Apagar el profiler por defecto en tests: los que lo prueban lo activan
    # explicitamente con monkeypatch.
    "QUERY_PROFILER_ENABLED": "false",
}

for _key, _value in _DUMMY_ENV.items():
    os.environ.setdefault(_key, _value)
