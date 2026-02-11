import requests

# ==========================================
# CONFIGURACIÓN - Propiedad "Sofía Activa"
# ==========================================
# Tu Access Token de la App Privada (pat-na1-...)
ACCESS_TOKEN = "pat-na1-cb39cafc-1eb4-4d21-9aea-2453402dfe7d"


def crear_propiedad_sofia_activa():
    """
    Crea una propiedad personalizada en HubSpot para controlar si Sofía
    debe responder o si el asesor humano tiene el control.

    Valores:
    - "true" (Sí): Sofía responde automáticamente
    - "false" (No): Sofía está silenciada, el humano tiene control
    """
    url = "https://api.hubapi.com/crm/v3/properties/contacts"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "name": "sofia_activa",
        "label": "Sofía Activa",
        "description": "Controla si el bot Sofía responde (Sí) o si un asesor humano tiene el control (No)",
        "groupName": "contactinformation",
        "type": "enumeration",
        "fieldType": "booleancheckbox",
        "options": [
            {
                "label": "Sí",
                "value": "true",
                "displayOrder": 0
            },
            {
                "label": "No",
                "value": "false",
                "displayOrder": 1
            }
        ]
    }

    print("🚀 Creando propiedad 'sofia_activa' en HubSpot...")

    try:
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            print("\n✅ ¡PROPIEDAD CREADA CON ÉXITO!")
            print("-" * 50)
            print("Nombre interno: sofia_activa")
            print("Tipo: Checkbox (Sí/No)")
            print("-" * 50)
            print("\nAhora los asesores pueden:")
            print("1. Ir al contacto en HubSpot")
            print("2. Cambiar 'Sofía Activa' a 'No' para silenciar al bot")
            print("3. El middleware detectará el cambio y pausará a Sofía")

        elif response.status_code == 409:
            print("\n⚠️ La propiedad 'sofia_activa' ya existe.")
            print("Puedes usarla directamente en HubSpot.")

        else:
            print(f"\n❌ Error {response.status_code}: {response.text}")

    except Exception as e:
        print(f"\n❌ Error de conexión: {str(e)}")


if __name__ == "__main__":
    if "TU_ACCESS_TOKEN" in ACCESS_TOKEN:
        print("❌ ERROR: Configura el ACCESS_TOKEN antes de continuar.")
    else:
        crear_propiedad_sofia_activa()