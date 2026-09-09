from twilio.rest import Client
import os
from dotenv import load_dotenv

load_dotenv()

client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

print("--- 🎯 VERIFICANDO EL SERVICIO PREDETERMINADO ---")

try:
    # Según tu 'dir', el objeto tiene un método 'get'
    config = client.conversations.v1.configuration.get().fetch()
    print(f"✅ El IS que manda en esta cuenta es: {config.default_chat_service_sid}")
    
    if config.default_chat_service_sid == 'ISc6359d2689cb4a45ad4fb9d8ac2f6b80':
        print("Resultado: Es el servicio 'SofIA_Proteger'.")
    elif config.default_chat_service_sid == 'IS9deed03f396945f88f7f0dd1496a371a':
        print("Resultado: Es el 'Default Conversations Service'.")
    else:
        print("Resultado: Es un tercer servicio no identificado.")

except Exception as e:
    print(f"❌ Error al consultar: {e}")