import os
from time import time
import cloudinary.uploader
from openai import AsyncOpenAI

client_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def process_whatsapp_image(media_url: str, phone: str):
    # 1. Subir a Cloudinary (Almacenamiento permanente)
    # Cloudinary puede descargar directamente desde la URL de Twilio si le pasas el link
    upload_result = cloudinary.uploader.upload(
        media_url, 
        folder="proteger_inmuebles",
        public_id=f"img_{phone}_{int(time.time())}",
        resource_type="image"
    )
    permanent_url = upload_result["secure_url"]

    # 2. Análisis Visual con GPT-4o-mini
    # Le enviamos la URL permanente para que Sofía "vea" el inmueble
    response = await client_openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analiza esta imagen de una propiedad. Identifica si hay algún código visible, el tipo de inmueble (casa, apto, local) y características clave como estado de la fachada o acabados."},
                    {"type": "image_url", "image_url": {"url": permanent_url}}
                ],
            }
        ],
        max_tokens=300,
    )
    
    analysis_text = response.choices.message.content

    return {
        "url_permanente": permanent_url,
        "analisis_visual": analysis_text
    }
    
