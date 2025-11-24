from rag import rag_service
import os

print('=' * 60)
print('CRITERIO 2: Actualizacion Real (A -> B)')
print('=' * 60)

test_file = 'knowledge_base/test_criterio2.txt'

print('\n[Paso 1] Crear archivo con respuesta A')
with open(test_file, 'w', encoding='utf-8') as f:
    f.write('Pregunta: Cual es la respuesta?\n')
    f.write('Respuesta: A\n')

print('[Paso 2] Recargar y buscar A')
rag_service.reload_knowledge_base()
result_A = rag_service.search_knowledge('knowledge_base/test_criterio2.txt', 'respuesta')
assert 'Respuesta: A' in result_A
print('OK: Sistema responde A')

print('\n[Paso 3] Modificar a respuesta B')
with open(test_file, 'w', encoding='utf-8') as f:
    f.write('Pregunta: Cual es la respuesta?\n')
    f.write('Respuesta: B\n')

print('[Paso 4] Recargar y buscar B')
rag_service.reload_knowledge_base()
result_B = rag_service.search_knowledge('knowledge_base/test_criterio2.txt', 'respuesta')
assert 'Respuesta: B' in result_B
assert 'Respuesta: A' not in result_B
print('OK: Sistema responde B')

print('\n[Paso 5] Limpiar')
os.remove(test_file)
rag_service.reload_knowledge_base()

print('\nCRITERIO 2 VALIDADO')
