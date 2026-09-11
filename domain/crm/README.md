# Contratos de dominio del mini CRM

Estos modelos definen el vocabulario interno que queremos estabilizar antes de
seguir agregando funcionalidades.

## Entidades base

- `CRMContact`: contacto propio del CRM.
- `CRMConversation`: conversacion por canal y estado.
- `CRMMessage`: mensaje entrante, saliente o de sistema.
- `CRMAppointment`: cita asociada a contacto y asesor.
- `CRMLeadStage`: etapa del lead.
- `CRMAdvisor`: miembro del equipo.
- `CRMEvent`: evento interno para auditoria y automatizaciones.

## Regla

Los contratos no deben depender de FastAPI, MongoDB, Redis, HubSpot, Twilio,
Bunny ni OpenAI. Las dependencias externas entran por adaptadores/servicios, no
por el dominio.

## Uso esperado

1. Las rutas reciben HTTP.
2. Los servicios ejecutan casos de uso.
3. Los adaptadores traducen hacia proveedores externos o bases propias.
4. El dominio conserva nombres y estados estables.
