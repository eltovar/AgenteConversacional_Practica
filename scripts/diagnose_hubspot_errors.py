#!/usr/bin/env python3
"""
ANÁLISIS FINAL: ¿Por qué persiste el error PROPERTY_DOESNT_EXIST?

Este script examina todos los lugares donde se actualiza HubSpot
y verifica si están protegidos con try/except.
"""

import os
import re

def analyze_file(filepath, search_patterns):
    """Busca patrones en un archivo y retorna contexto"""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            lines = content.split('\n')
        
        results = []
        for pattern_name, pattern in search_patterns.items():
            matches = re.finditer(pattern, content, re.MULTILINE)
            for match in matches:
                line_num = content[:match.start()].count('\n') + 1
                results.append({
                    'pattern': pattern_name,
                    'line': line_num,
                    'text': lines[line_num-1].strip()
                })
        
        return results
    except Exception as e:
        return []

# Puntos críticos a analizar
files_to_check = {
    'middleware/webhook_handler.py': {
        'update_contact_info_calls': r'await contact_manager\.update_contact_info\(',
        'hubspot_update_direct': r'await.*hubspot\.update_contact\(',
        'optional_properties_block': r'optional_properties\s*=\s*\{',
        'try_except_pairs': r'try:|except.*Exception',
    },
    'middleware/contact_manager.py': {
        'update_contact_info_def': r'async def update_contact_info\(',
        'hubspot_update_call': r'await self\.hubspot\.update_contact\(',
        'property_doesnt_exist_check': r'PROPERTY_DOESNT_EXIST',
        'raise_statements': r'raise',
    },
    'agents/CRMAgent/crm_agent.py': {
        'hubspot_update_direct': r'await self\.hubspot\.update_contact\(',
        'create_contact_call': r'await self\.hubspot\.create_contact\(',
        'try_except_pairs': r'try:|except',
    },
    'integrations/hubspot/hubspot_client.py': {
        'update_contact_method': r'async def update_contact\(',
        'error_handling': r'except.*HTTPStatusError',
    }
}

print("=" * 80)
print("🔍 DIAGNÓSTICO: Ubicaciones donde se actualiza HubSpot")
print("=" * 80)

base_path = r"c:\Users\Salo\Desktop\AgenteConversacional_Practica"

for filepath, patterns in files_to_check.items():
    full_path = os.path.join(base_path, filepath)
    
    if not os.path.exists(full_path):
        print(f"\n❌ Archivo no encontrado: {filepath}")
        continue
    
    print(f"\n📄 {filepath}")
    print("-" * 80)
    
    results = analyze_file(full_path, patterns)
    
    if not results:
        print("   (No se encontraron puntos críticos)")
        continue
    
    for result in sorted(results, key=lambda x: x['line']):
        print(f"   L{result['line']:4d}: [{result['pattern']:25s}] {result['text'][:70]}")

print("\n" + "=" * 80)
print("⚠️  INDICADORES CRÍTICOS A VERIFICAR")
print("=" * 80)

print("""
1. ¿ webhook_handler.py TIENE try/except para base_properties?
   ✅ Sí → Líneas 1197-1211
   
2. ¿ webhook_handler.py TIENE try/except para optional_properties?
   ✅ Sí → Líneas 1246-1255

3. ¿ contact_manager.py relanza PROPERTY_DOESNT_EXIST?
   ✅ Sí → Líneas 518-522

4. ¿ agents/CRMAgent/crm_agent.py TIENE try/except en update_contact?
   ❓ REVISAR → Línea 568

5. ¿ Hay otros update_contact calls sin protección?
   ❓ REVISAR → Grep results abajo
""")

# Búsqueda final: todos los update_contact sin try
print("\n" + "=" * 80)
print("🔍 BÚSQUEDA: ¿Hay update_contact calls SIN try/except?")
print("=" * 80)

webhook_content = open(os.path.join(base_path, 'middleware/webhook_handler.py')).read()
crm_content = open(os.path.join(base_path, 'agents/CRMAgent/crm_agent.py')).read()

print("\n📄 webhook_handler.py - update_contact calls:")
for match in re.finditer(r'update_contact_info\(', webhook_content):
    line_num = webhook_content[:match.start()].count('\n') + 1
    # Verificar si está dentro de try
    text_before = webhook_content[max(0, match.start()-200):match.start()]
    has_try = 'try:' in text_before and text_before.count('try:') > text_before.count('except')
    status = "✅ EN TRY" if has_try else "⚠️  SIN TRY?"
    print(f"   L{line_num}: {status}")

print("\n📄 crm_agent.py - update_contact calls:")
for match in re.finditer(r'update_contact\(', crm_content):
    line_num = crm_content[:match.start()].count('\n') + 1
    text_before = crm_content[max(0, match.start()-200):match.start()]
    has_try = 'try:' in text_before and text_before.count('try:') > text_before.count('except')
    status = "✅ EN TRY" if has_try else "🔴 SIN TRY"
    print(f"   L{line_num}: {status}")

print("\n" + "=" * 80)
print("📋 CONCLUSIÓN")
print("=" * 80)
print("""
Si los tests siguen fallando con error 400 PROPERTY_DOESNT_EXIST:

Opción A: El error viene de crm_agent.py (line 568)
   → Falta protección con try/except

Opción B: El error viene de webhook_handler.py pero no se está relanzando
   → Revisar si str(e) contiene "PROPERTY_DOESNT_EXIST"
   → Podría ser un problema de formato del mensaje de error

Opción C: Hay otro update_contact call sin protección
   → Ver grep results arriba

Próximo paso: Agregar try/except a crm_agent.py línea 568
""")
