# Script de limpieza del Vector Store pgvector
# Elimina la colección completa y la recrea vacía

Write-Host "🧨 LIMPIEZA TOTAL DEL VECTOR STORE" -ForegroundColor Yellow
Write-Host "=================================" -ForegroundColor Yellow

# Cargar variables de entorno del archivo .env
Write-Host "📂 Cargando variables de entorno..." -ForegroundColor Cyan
Get-Content .\.env | ForEach-Object {
    if ($_ -match "^\s*([^=]+)=(.*)$") {
        $name = $matches[1].Trim()
        $value = $matches[2].Trim().Trim('"')
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

Write-Host "✅ Variables cargadas" -ForegroundColor Green
Write-Host "   DATABASE_URL: $($env:DATABASE_URL.Substring(0, [Math]::Min(50, $env:DATABASE_URL.Length)))..." -ForegroundColor DarkGray
Write-Host "   VECTOR_COLLECTION_NAME: $($env:VECTOR_COLLECTION_NAME -or 'rag_knowledge_base (default)')" -ForegroundColor DarkGray

# Usar la colección por defecto si no está definida
if (-not $env:VECTOR_COLLECTION_NAME) {
    $env:VECTOR_COLLECTION_NAME = "rag_knowledge_base"
}

$env:PYTHONPATH = "."

Write-Host "`n🔌 Inicializando conexión a PostgreSQL + pgvector..." -ForegroundColor Cyan

python - << 'EOF'
import os
import sys
from rag.vector_store import pg_vector_store

try:
print("\n🔌 Inicializando conexión...")
pg_vector_store.initialize_db()
    
print("\n🔥 Eliminando colección pgvector...")
pg_vector_store.delete_collection()
    
print("\n✅ Colección eliminada correctamente")
print("🎯 El vector store está listo para reindexación\n")
    
except Exception as e:
print(f"\n❌ Error: {e}", file=sys.stderr)
sys.exit(1)
EOF

if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ Script completado exitosamente" -ForegroundColor Green
}
else {
    Write-Host "❌ Error durante la ejecución" -ForegroundColor Red
    exit 1
}
