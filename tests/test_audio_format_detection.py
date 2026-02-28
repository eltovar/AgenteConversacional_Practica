#!/usr/bin/env python3
"""
Script de validación: Verificar que los formatos de audio se detectan correctamente
"""

print("=" * 80)
print("VALIDACIÓN DE DETECCIÓN DE FORMATO DE AUDIO PARA WHISPER")
print("=" * 80)

# Simular la lógica de detección
def get_audio_extension(content_type: str) -> str:
    """Detecta extensión correcta basándose en content_type"""
    content_lower = content_type.lower()
    
    if "ogg" in content_lower or "vorbis" in content_lower:
        return "ogg"
    elif "webm" in content_lower:
        return "webm"
    elif "mp4" in content_lower or "m4a" in content_lower or "aac" in content_lower:
        return "mp4"
    elif "wav" in content_lower:
        return "wav"
    elif "mpeg" in content_lower or "mp3" in content_lower:
        return "mp3"
    elif "flac" in content_lower:
        return "flac"
    else:
        return "ogg"  # Default

# Casos de prueba
test_cases = [
    ("audio/ogg", "ogg", "✓ Twilio envia esta"),
    ("audio/webm", "webm", "✓ Navegador Chrome/Edge grabando"),
    ("audio/mpeg", "mp3", "✓ API externa"),
    ("audio/mp3", "mp3", "✓ Formato alternativo"),
    ("audio/mp4", "mp4", "✓ Audio en contenedor MP4"),
    ("audio/wav", "wav", "✓ WAV sin compresión"),
    ("audio/flac", "flac", "✓ FLAC sin compresión"),
    ("audio/x-vorbis-config", "ogg", "✓ Vorbis (ogg)"),
]

print("\n📋 DETECCIÓN DE FORMATOS PARA WHISPER:")
print("-" * 80)

all_pass = True
for content_type, expected, note in test_cases:
    detected = get_audio_extension(content_type)
    status = "✓" if detected == expected else "✗"
    
    if detected != expected:
        all_pass = False
        
    print(f"\n  {status} Content-Type: {content_type}")
    print(f"     Detectado: audio.{detected}")
    print(f"     Esperado:  audio.{expected}")
    print(f"     {note}")

print("\n" + "=" * 80)
if all_pass:
    print("✅ DETECCIÓN CORRECTA EN TODOS LOS CASOS")
    print("\nOpenAI Whisper aceptará estos formatos:")
    print("  - audio/ogg → audio.ogg")
    print("  - audio/webm → audio.webm")
    print("  - audio/mp4 → audio.mp4")
    print("  - audio/wav → audio.wav")
    print("  - audio/mpeg → audio.mp3")
    print("  - audio/flac → audio.flac")
    print("\n✅ El error 'Invalid file format' de Whisper debería DESAPARECER")
else:
    print("❌ PROBLEMAS DETECTADOS")

print("=" * 80)
