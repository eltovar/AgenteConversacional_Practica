from twilio.rest import Client
import os
from dotenv import load_dotenv

load_dotenv()

client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
OLD_IS = 'IS9deed03f396945f88f7f0dd1496a371a'

print(f"--- 🧹 LIMPIEZA GRADUAL DE {OLD_IS} ---")

# Listar conversaciones activas en el servicio viejo
conversations = client.conversations.v1.services(OLD_IS).conversations.list(state="active")

if not conversations:
    print("No hay conversaciones activas en el servicio viejo.")
else:
    for conv in conversations:
        print(f"\nID: {conv.sid}")
        print(f"Nombre/Tel: {conv.friendly_name}")
        print(f"Última actividad: {conv.date_updated}")
        
        confirmar = input("¿Deseas cerrar esta conversación? (s/n): ")
        
        if confirmar.lower() == 's':
            client.conversations.v1.services(OLD_IS).conversations(conv.sid).update(state='closed')
            print(f"✅ {conv.sid} cerrada con éxito.")
        else:
            print(f"⏭️ Saltada.")

print("\n--- Proceso terminado ---")