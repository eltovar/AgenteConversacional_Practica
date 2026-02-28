#!/usr/bin/env python3
"""
Script de validación: Detección de HTML/Error en lugar de audio
Explica por qué Twilio está devolviendo HTML en lugar del archivo real
"""

print("=" * 80)
print("ANÁLISIS: ¿Por qué Whisper recibe HTML en lugar de audio?")
print("=" * 80)

# Convertir los magic bytes hexadecimales a ASCII
magic_hex = "3c21444f4354595045206874"
magic_bytes = bytes.fromhex(magic_hex)
readable = magic_bytes.decode('ascii', errors='replace')

print(f"\n📋 MAGIC BYTES DEL ARCHIVO DESCARGADO:")
print(f"   Hexadecimal: {magic_hex}")
print(f"   ASCII: {readable}")
print(f"   ➜ Esto es: <!DOCTYPE ht... (comienzo de HTML)")

print("\n" + "=" * 80)
print("CAUSAS PROBABLES")
print("=" * 80)

causes = """
El cliente envía audio a WhatsApp → Twilio lo procesa → Sofia intenta descargarlo

Pero Twilio devuelve HTML en lugar del audio (probable página de error):

1. ❌ URL DE MEDIA EXPIRADA
   - Twilio genera URLs temporales para archivos media
   - Después de ~24-48 horas, expiran
   - Si el cliente intenta descargar después, obtiene 404 → error page HTML

2. ❌ ARCHIVO ELIMINADO EN TWILIO
   - Twilio borra archivos después de cierto período (típicamente 30 días)
   - Intento de descargar archivo ya borrado → error page HTML

3. ❌ ERROR DE AUTENTICACIÓN
   - Credenciales TWILIO_ACCOUNT_SID o TWILIO_AUTH_TOKEN incorrectas
   - Twilio rechaza la petición → devuelve página login/error HTML

4. ❌ PROBLEMA DE RED TWILIO
   - Timeout temporal en Twilio
   - Error en servidor → devuelve página de error 502/503 HTML

5. ⚠️ TWILIO ENVIANDO MEDIAS INCORRECTAS
   - El cliente grabó un audio pero Twilio lo guardó como HTML
   - Bug interno de Twilio (raro pero posible)
"""

print(causes)

print("=" * 80)
print("SOLUCIÓN IMPLEMENTADA")
print("=" * 80)

solution = """
✅ NUEVA DETECCIÓN DE ERRORES:

La función detect_audio_format_by_magic_bytes() ahora:

1. Lee primeros 12 bytes del archivo descargado
2. Si encuentra HTML/XML: LANZA EXCEPCIÓN con mensaje descriptivo
3. La excepción es capturada y reportada en logs

ANTES (❌ Silencioso):
   - Magic bytes: <!DOCTYPE
   - Código asume: "formato desconocido, asumo OGG"
   - Guarda como .ogg
   - Envía HTML a Whisper → Error 400 sin contexto

AHORA (✅ Diagnóstico claro):
   - Magic bytes: <!DOCTYPE
   - Detecta: "esto es HTML, no audio"
   - LANZA: "Error descargando audio de Twilio: El servidor devolvió HTML/XML.
             Probable causa: URL expirada, archivo eliminado, o error de 
             autenticación en Twilio"
   - Logs muestran exactamente qué está mal
   - Los primeros 100 bytes del HTML se loguean para debugging

PRÓXIMOS PASOS (PARA TI):

1. Revisa el log: busca el mensaje de error sobre HTML
2. Identifica la causa:
   ✓ ¿URLs expiradas? → Descarga media más rápido o usa media local
   ✓ ¿Auth fallida? → Verifica TWILIO_ACCOUNT_SID y TWILIO_AUTH_TOKEN
   ✓ ¿Problema de red? → Intenta de nuevo (puede ser temporal)
3. En los logs verás los primeros 100 bytes del HTML para más contexto
"""

print(solution)
print("=" * 80)
