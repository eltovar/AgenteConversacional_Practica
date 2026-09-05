# Flujo Git para Staging

Fecha: 2026-09-04

## Evidencia local

| Campo | Resultado |
|---|---|
| Rama local actual | `dev_juanrodriguez` |
| Commit local | `f958cba87f9398f1b6c7b31abb6a2482dfc16fb3` |
| Remote | `origin` -> `https://github.com/eltovar/AgenteConversacional_Practica.git` |
| Ramas locales | `dev_juanrodriguez`, `main` |
| Ramas remotas visibles | `origin/main`, `origin/dev_juanrodriguez`, `origin/claude/amazing-maxwell-kzd558`, `origin/claude/conversations-fallback-compliance` |
| Rama `staging` | No existe local ni remota en evidencia actual |
| Commit production | No verificado |
| Commit staging | No verificado |

## Estrategia documentada

El plan recomienda:

```text
main/master -> production
staging     -> staging
```

## Estrategia real verificada

No verificada. Falta vincular Railway o consultar el dashboard para saber:

- que rama despliega production;
- que rama despliega staging;
- que commit esta desplegado en production;
- que commit esta desplegado en staging;
- si existe environment staging;
- si el proyecto se llama efectivamente `caring-balance`.

## Riesgos

1. No existe rama `staging` visible; crearla sin revisar Railway podria no conectar con nada.
2. Si Railway production despliega desde `main`, la rama actual `dev_juanrodriguez` no debe desplegar directo a production.
3. Si staging se crea copiando variables desde production, la rama correcta no basta: los servicios externos seguirian siendo reales.

## Flujo recomendado

```text
feature/* o dev_juanrodriguez
  -> tests locales
  -> Pull Request hacia staging
  -> deploy environment Staging
  -> matriz de regresion
  -> Pull Request hacia main
  -> deploy Production
```

## Plan exacto de implementacion posterior a esta auditoria

No ejecutar hasta revisar y aprobar esta auditoria.

1. Vincular Railway en este repo o ejecutar comandos de lectura con `--project <PROJECT_ID>` del proyecto correcto.
2. Auditar `production` y `staging` reales: servicios, volumenes, dominios, variables por nombre, rama y commit desplegado.
3. Crear rama `staging` solo si Railway no tiene una estrategia valida ya existente.
4. Crear o validar servicios independientes en environment `staging`: MongoDB, Redis, PostgreSQL/pgvector y API.
5. Configurar variables staging con credenciales propias, sin copiar URLs productivas.
6. Implementar compuertas de seguridad en codigo:
   - environment central `APP_ENV`;
   - outbound Twilio OFF por defecto en staging;
   - HubSpot writes OFF por defecto en staging;
   - scheduler outbound OFF por defecto en staging;
   - allowlist de numeros de prueba;
   - prefijo/zona Bunny staging;
   - healthcheck con environment y checks separados.
7. Agregar seed idempotente `TEST_*` y reset seguro limitado a `TEST_*`.
8. Crear sentinel `SOFIA_STAGING_SENTINEL` para Mongo, Redis y Postgres.
9. Encender staging en orden: MongoDB, Redis, PostgreSQL/pgvector, API.
10. Ejecutar smoke tests y matriz de regresion.
11. Documentar costos base y procedimiento de apagado.

## Criterio de listo

No declarar `STAGING = VALIDADO` hasta confirmar con evidencia:

- API aislada.
- MongoDB aislado.
- Redis aislado.
- PostgreSQL/pgvector aislado.
- Datos `TEST_*`.
- Twilio controlado.
- HubSpot controlado.
- Scheduler controlado.
- RAG validado.
- WebSocket aislado.
- Sentinel aprobado.
- Healthcheck remoto aprobado.
