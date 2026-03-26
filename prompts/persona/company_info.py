# prompts/persona/company_info.py
"""
Información de la empresa: datos de contacto, horarios, cobertura geográfica,
instrucciones de búsqueda de inmuebles y directorio de departamentos.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# DATOS BÁSICOS DE LA EMPRESA
# ═══════════════════════════════════════════════════════════════════════════════

COMPANY_BASICS = """INMOBILIARIA PROTEGER — INFORMACIÓN GENERAL
Dirección  : Calle 36 sur #41-37, Primer piso, Envigado, Antioquia
Teléfono   : 57 321 817 5110 | Fijo: 604 444 63 64
Gerencia   : gerencia.inmproteger@gmail.com
Web        : www.inmobiliariaproteger.com
Horarios   : Lunes a viernes 8:30 a.m. – 5:00 p.m. | Sábados 8:30 a.m. – 12:00 p.m."""

# ═══════════════════════════════════════════════════════════════════════════════
# COBERTURA GEOGRÁFICA
# ═══════════════════════════════════════════════════════════════════════════════

COMPANY_COVERAGE = """COBERTURA GEOGRÁFICA — ÁREA METROPOLITANA DE ANTIOQUIA
Operamos exclusivamente en:
- Valle de Aburrá: Medellín, Bello, Copacabana, Girardota, Barbosa
- Sur: Itagüí, Sabaneta, Envigado, La Estrella, Caldas

Fuera de esta área → informar amablemente que solo operamos en el Área Metropolitana."""

# ═══════════════════════════════════════════════════════════════════════════════
# INSTRUCCIONES PARA BÚSQUEDA DE INMUEBLES POR CÓDIGO
# ═══════════════════════════════════════════════════════════════════════════════

PROPERTY_SEARCH_INSTRUCTIONS = """CÓMO BUSCAR UN INMUEBLE POR CÓDIGO
Si el cliente tiene el código de un inmueble:
1. Dile el código que tiene
2. Indícale que ingrese a www.inmobiliariaproteger.com
3. Use el buscador (ícono de lupa) en la página para encontrarlo
4. Si no lo encuentra o quiere más información, un Asesor Comercial puede ayudarle"""

# ═══════════════════════════════════════════════════════════════════════════════
# DIRECTORIO DE DEPARTAMENTOS
# ═══════════════════════════════════════════════════════════════════════════════

CONTACT_DIRECTORY = """DIRECTORIO DE CONTACTOS — INMOBILIARIA PROTEGER

Gerencia
  Email: gerencia.inmproteger@gmail.com

Asesores Comerciales (compra, venta, arriendo de inmuebles)
  Sebastian · Cel: 318 377 2859 · WhatsApp general: 57 321 817 5110

Caja (pagos, consignaciones, certificados de renta)
  WhatsApp: 604 444 63 64

Administraciones (cuotas residenciales, multas, convivencia)
  WhatsApp: 320 609 28 96

Contabilidad (facturas, certificados tributarios, retenciones)
  WhatsApp: 604 444 63 64

Contratos (terminación, prórroga, documentación)
  WhatsApp: 604 444 63 64

Cartera (cobros, mora, acuerdos de pago)
  WhatsApp: 310 515 5781

Jurídico (Data Crédito, demandas, codeudores)
  WhatsApp: 321 789 86 79

Servicios Públicos (EPM, financiación, revisión gas)
  WhatsApp: 323 508 18 84

Reparaciones y Mantenimiento
  WhatsApp: 323 327 7132
  Coordinación — Mateo · Cel: 323 515 8007
  Email: reparaciones@inmobiliariaproteger.com

Reparaciones — Oficiales (ejecución en campo)
  Carlos · Cel: 304 497 6108

El Libertador (estudio de crédito para arriendo — 100% digital)
  Sin costo · Proceso en línea: ingresa al link y llena tus datos"""

# ═══════════════════════════════════════════════════════════════════════════════
# TEXTO COMPUESTO PARA USO EN PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

COMPANY_CONTEXT = f"{COMPANY_BASICS}\n\n{COMPANY_COVERAGE}\n\n{CONTACT_DIRECTORY}"
