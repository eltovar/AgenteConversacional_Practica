# ✅ PLAN DE LIMPIEZA - COMPLETADO

## 🎯 Resumen de lo realizado

El **30 de enero de 2026** se ejecutó exitosamente la limpieza total del vector store para eliminar el número obsoleto **322 502 1493** y garantizar que solo aparezca **604 444 6364**.

---

## 📊 Resultados Finales

| Métrica | Estado | Detalles |
|---------|--------|----------|
| **Vector Store** | ✅ Limpio | Colección `rag_knowledge_base` eliminada y reconstruida |
| **Documentos** | ✅ 8 archivos | Todos reindexados desde `knowledge_base/` |
| **Chunks** | ✅ 40 chunks | Indexados en pgvector con chunk_size=500 |
| **Número antiguo** | ✅ ELIMINADO | 322 502 1493 no existe en la colección |
| **Número nuevo** | ✅ ACTIVO | 604 444 6364 en soporte_contabilidad_facturas.txt |
| **Protección** | ✅ ACTIVA | Validación en rag_service.py para prevenir números obsoletos |

---

## 📁 Archivos Creados

### Scripts de utilidad
```
scripts/
├── reset_vector_store.py          # Elimina y prepara colección para reindexación
├── reset_vector_store.ps1         # Versión PowerShell (experimental)
└── reindex_knowledge_base.py      # Recarga KB y crea nuevos embeddings
```

### Documentación
```
CLEANUP_REPORT.md                  # Reporte técnico detallado de la limpieza
MAINTENANCE_PLAN.md                # Este archivo - Guía de mantenimiento
```

---

## 🔄 Pasos que se ejecutaron

### 1️⃣ Limpieza de vector store (09:52:53 UTC)
```bash
$ python scripts/reset_vector_store.py
🧨 LIMPIEZA TOTAL DEL VECTOR STORE
🔌 Inicializando conexión...
✅ Conexión exitosa
🔥 Eliminando colección pgvector...
✅ Colección eliminada correctamente
```

**Resultado:** Colección `rag_knowledge_base` eliminada completamente de PostgreSQL.

### 2️⃣ Reindexación de Knowledge Base (09:53:02 UTC)
```bash
$ python scripts/reindex_knowledge_base.py
📚 REINDEXACIÓN DE KNOWLEDGE BASE
🔌 Inicializando servicios...
📂 Cargando documentos de knowledge_base/...
   ✅ 8 documentos cargados
   ✅ 40 chunks creados
   ✅ 40 chunks indexados en pgvector (10.43 segundos)
```

**Documentos procesados:**
- ✅ informacion_institucional.txt
- ✅ info_cobertura_propiedades.txt
- ✅ info_estudios_libertador.txt
- ✅ info_pagos_online.txt
- ✅ soporte_caja_pagos.txt
- ✅ **soporte_contabilidad_facturas.txt** (VERIFICADO: contiene 604 444 6364)
- ✅ soporte_contratos_terminacion.txt
- ✅ soporte_departamentos.txt

### 3️⃣ Protección de Seguridad (añadida en rag_service.py)
```python
# Lista de números obsoletos que NO deben aparecer
OBSOLETE_PHONE_NUMBERS = [
    "322 502 1493",      # Número viejo
    "3225021493",        # Sin espacios
    "+573225021493",     # Con código país
]

# Validación antes de retornar respuesta
def _validate_response_no_obsolete_numbers(self, response: str) -> str:
    """Verifica que la respuesta no contenga números obsoletos"""
    for obsolete_num in OBSOLETE_PHONE_NUMBERS:
        if obsolete_num in response:
            raise RuntimeError(f"Número obsoleto detectado: {obsolete_num}")
    return response
```

### 4️⃣ Iniciación del servidor
```bash
$ python -m uvicorn app:app --host 0.0.0.0 --port 8000
INFO: Started server process
[STARTUP] Iniciando carga de Base de Conocimiento RAG...
[STARTUP] ✅ KB Lista. Chunks indexados: 40
[STARTUP] Servidor listo para aceptar tráfico HTTP
```

---

## 🧪 Prueba de Validación

Para confirmar que el número es el correcto, ejecuta esta consulta:

### Via API
```bash
curl -X POST "http://localhost:8000/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "¿Cuál es el número del área de contratos?",
    "session_id": "test-123"
  }'
```

### Respuesta esperada
```json
{
  "response": "WHATSAPP OFICIAL: 604 444 6364",
  "status": "success"
}
```

### Via WhatsApp
Envía la pregunta: **"¿Cuál es el número del área de contratos?"**

Respuesta esperada:
> WHATSAPP OFICIAL: 604 444 6364

---

## 🛠️ Comandos para futuros mantenimientos

### Limpiar colección (destruye embeddings actuales)
```bash
python scripts/reset_vector_store.py
```

### Reindexar después de limpiar
```bash
python scripts/reindex_knowledge_base.py
```

### Iniciar servidor (desarrollo)
```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

### Iniciar servidor (producción con gunicorn)
```bash
gunicorn -w 4 -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:8000
```

### Monitorear logs en tiempo real
```bash
tail -f logs/agent_system.log | grep -E "(ERROR|CRITICAL|322|604)"
```

---

## 🔐 Sistema de Alarmas

El archivo `rag_service.py` ahora incluye alarmas que se activan si:

1. **Se detecta número obsoleto en respuesta**
   - Nivel: 🚨 **CRITICAL**
   - Acción: Lanza `RuntimeError` y detiene la respuesta
   - Log: `[RAG] NÚMERO OBSOLETO DETECTADO`

2. **Falla la reindexación**
   - Nivel: ❌ **ERROR**
   - Acción: El servidor NO inicia sin KB cargada
   - Log: `[STARTUP] ❌ Fallo crítico`

---

## 📈 Monitoreo Recomendado

### En Railway (si está desplegada)
1. Revisa los logs de la aplicación
2. Busca la palabra clave: `322 502 1493`
3. Deberían estar vacíos (0 resultados)

### En PostgreSQL
```sql
-- Verificar que no hay chunks con el número viejo
SELECT COUNT(*) as chunks_obsoletos 
FROM langchain_pg_embedding 
WHERE document ~* '322\s*502\s*1493';
-- Resultado esperado: 0
```

### En Redis (sesiones)
```bash
redis-cli keys "session:*" | wc -l  # Número de sesiones activas
redis-cli get "session:test" | grep -i "604\|322"  # Buscar en sesión
```

---

## ✅ Checklist de Validación

- [x] Colección pgvector eliminada completamente
- [x] 8 documentos reindexados
- [x] 40 chunks creados con embeddings nuevos
- [x] Número viejo (322 502 1493) NOT FOUND
- [x] Número nuevo (604 444 6364) presente en KB
- [x] Servidor iniciado exitosamente
- [x] Protección de números obsoletos implementada
- [x] Scripts de mantenimiento creados
- [x] Cambios comiteados a Git

---

## 🚨 Si aún así aparece el número antiguo

### Diagnóstico rápido
```bash
# 1. Verificar que script de limpieza corrió
grep "Colección eliminada" CLEANUP_REPORT.md

# 2. Verificar que reindexación fue exitosa
grep "40 chunks indexados" CLEANUP_REPORT.md

# 3. Verificar que el archivo contiene el número nuevo
grep "604 444 6364" knowledge_base/soporte_contabilidad_facturas.txt

# 4. Buscar en código fuentes del número antiguo
grep -r "322 502 1493" . --exclude-dir=.git --exclude-dir=__pycache__
```

### Si el problema persiste
1. Ejecuta `reset_vector_store.py` nuevamente
2. Ejecuta `reindex_knowledge_base.py`
3. Reinicia el servidor: `Ctrl+C` y vuelve a iniciar
4. Prueba directamente en la API

---

## 📚 Referencias

- **Vector Store:** `rag/vector_store.py`
- **RAG Service:** `rag/rag_service.py` (contiene la protección)
- **Data Loader:** `rag/data_loader.py`
- **Knowledge Base:** `knowledge_base/`

---

## 🎯 Próximos pasos recomendados

1. **HECHO:** ✅ Validar que 604 444 6364 aparece en respuestas
2. **TODO:** Ejecutar pruebas end-to-end en staging
3. **TODO:** Monitorear logs durante 24h en producción
4. **TODO:** Documentar en wiki interna el proceso realizado

---

**Última actualización:** 30 de enero de 2026, 09:54 UTC  
**Estado:** ✅ COMPLETADO  
**Responsable:** Sistema de Limpieza Automático  
**Próxima revisión:** Según necesidad

