"""Metrics service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

import os
from datetime import datetime, timedelta
from io import BytesIO
from typing import Dict, Optional

from fastapi import Header, Query, Request, HTTPException
from fastapi.responses import HTMLResponse

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    SOCIAL_MEDIA_CHANNELS,
    format_date_excel,
    format_phone_excel,
    format_status_excel,
    get_httpx_client,
    get_mongo_manager,
    sanitize_name,
    sanitize_text,
    templates,
)
from utils.safe_logging import safe_error, safe_id, safe_phone


async def get_social_media_metrics(
    days: int = Query(7, ge=1, le=30, description="Días a analizar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna métricas de leads de redes sociales.

    Este endpoint es para el analista de redes sociales que solo necesita
    ver estadísticas, no enviar mensajes ni ver conversaciones detalladas.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        import httpx
        from collections import defaultdict

        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
        if not hubspot_api_key:
            raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

        # Calcular rango de fechas
        from zoneinfo import ZoneInfo
        TIMEZONE = ZoneInfo("America/Bogota")
        now = datetime.now(TIMEZONE)
        since = now - timedelta(days=days)
        since_ms = int(since.timestamp() * 1000)

        # Buscar contactos de redes sociales en HubSpot
        url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{
                "filters": [
                    {
                        "propertyName": "canal_origen",
                        "operator": "IN",
                        "values": SOCIAL_MEDIA_CHANNELS
                    },
                    {
                        "propertyName": "createdate",
                        "operator": "GTE",
                        "value": since_ms
                    }
                ]
            }],
            "properties": [
                "createdate",
                "canal_origen",
                "firstname",
                "lastname",
                "phone",
                "chatbot_score",
                "lifecyclestage",
                "hs_lead_status",       # Motivo/status del lead
                "message",              # Mensaje inicial (si existe)
                "notes_last_updated",   # Notas recientes
            ],
            "limit": 100,
            "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}]
        }

        contacts = []
        after = None
        client = get_httpx_client()
        while True:
            page_payload = dict(payload)
            if after:
                page_payload["after"] = after

            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {hubspot_api_key}"},
                json=page_payload,
                timeout=15.0
            )

            if response.status_code != 200:
                logger.error(f"[Metrics] HubSpot error: {response.status_code} - {safe_error(response.text, 200)}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Error consultando HubSpot: {response.status_code}. Intenta de nuevo en unos minutos."
                )

            data = response.json()
            contacts.extend(data.get("results", []))

            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break

        logger.info(f"[Metrics] Total contactos obtenidos de HubSpot: {len(contacts)}")

        # Procesar métricas
        leads_by_channel = defaultdict(int)
        leads_by_day = defaultdict(int)
        contacts_by_channel = defaultdict(list)  # Lista de contactos por canal
        total_leads = len(contacts)

        for contact in contacts:
            props = contact.get("properties", {})

            # Por canal - sanitizado
            canal_raw = props.get("canal_origen", "desconocido")
            canal = sanitize_text(canal_raw).lower() or "desconocido"
            leads_by_channel[canal] += 1

            # Extraer y sanitizar nombre
            firstname = props.get("firstname", "")
            lastname = props.get("lastname", "")
            nombre_completo = sanitize_name(firstname, lastname)

            # Extraer y formatear teléfono
            phone = format_phone_excel(props.get("phone", ""))

            # Extraer fecha y formatear
            fecha_raw = props.get("createdate", "")

            # Extraer motivo (combinar hs_lead_status con message si existe)
            motivo_parts = []
            hs_lead_status = props.get("hs_lead_status", "")
            if hs_lead_status:
                motivo_parts.append(sanitize_text(hs_lead_status))
            message = props.get("message", "")
            if message:
                # Truncar mensaje a 100 caracteres
                msg_clean = sanitize_text(message)[:100]
                if msg_clean:
                    motivo_parts.append(msg_clean)

            motivo = " - ".join(motivo_parts) if motivo_parts else "Consulta general"

            # Extraer status y formatear
            status = format_status_excel(props.get("lifecyclestage", "lead"))

            # Score
            score_raw = props.get("chatbot_score", "")
            score = sanitize_text(str(score_raw)) if score_raw else "-"

            # Agregar a la lista de contactos por canal
            # LLAVES CONSISTENTES para toda la cadena
            contacts_by_channel[canal].append({
                "fecha": fecha_raw,                 # Se formatea en Excel
                "canal": canal.capitalize(),        # Canal ya sanitizado
                "nombre": nombre_completo,          # Ya sanitizado
                "telefono": phone,                  # Ya formateado
                "motivo": motivo,                   # Nuevo campo
                "status": status,                   # Ya formateado
                "score": score,                     # Sanitizado
            })

            # Por día — convertir a zona Bogotá para que el gráfico coincida con
            # la percepción local (leads de 7pm-11pm no aparecen al día siguiente)
            createdate = props.get("createdate")
            if createdate:
                try:
                    dt_utc = datetime.fromisoformat(createdate.replace("Z", "+00:00"))
                    dt_bog = dt_utc.astimezone(TIMEZONE)
                    day_key = dt_bog.strftime("%Y-%m-%d")
                    leads_by_day[day_key] += 1
                except Exception:
                    pass

        # Ordenar leads por día
        leads_by_day_sorted = dict(sorted(leads_by_day.items()))

        # Log para debug - verificar datos extraídos
        logger.info(f"[Metrics] Total leads encontrados: {total_leads}")
        logger.info(f"[Metrics] Leads por canal: {dict(leads_by_channel)}")
        for canal, contactos in contacts_by_channel.items():
            logger.info(f"[Metrics] Canal '{canal}': {len(contactos)} contactos")
            if contactos:
                # Mostrar primer contacto como ejemplo
                ejemplo = contactos[0]
                logger.info(f"[Metrics] Ejemplo contacto: nombre={safe_id(ejemplo.get('nombre'), 'name')}, tel={safe_phone(ejemplo.get('telefono'))}")

        return {
            "period_days": days,
            "since": since.isoformat(),
            "until": now.isoformat(),
            "total_leads": total_leads,
            "leads_by_channel": dict(leads_by_channel),
            "leads_by_day": leads_by_day_sorted,
            "_contacts_by_channel": dict(contacts_by_channel),  # Interno: solo usado por export-excel
            "channels_tracked": SOCIAL_MEDIA_CHANNELS
        }

    except Exception as e:
        logger.error(f"[Metrics] Error obteniendo métricas: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def export_metrics_csv(
    days: int = Query(7, ge=1, le=30, description="Días a analizar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Exporta métricas de redes sociales a formato CSV.

    Genera un archivo CSV descargable con:
    - Resumen por canal de origen
    - Leads por día
    """
    from fastapi.responses import Response
    from io import StringIO
    import csv

    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Obtener datos de métricas
    metrics_data = await get_social_media_metrics(days=days, x_api_key=x_api_key)

    # Crear CSV en memoria
    output = StringIO()
    writer = csv.writer(output)

    # Sección: Resumen
    writer.writerow(["=== MÉTRICAS DE REDES SOCIALES ==="])
    writer.writerow([f"Periodo: últimos {days} días"])
    writer.writerow([f"Desde: {metrics_data['since'][:10]}"])
    writer.writerow([f"Hasta: {metrics_data['until'][:10]}"])
    writer.writerow([f"Total leads: {metrics_data['total_leads']}"])
    writer.writerow([])

    # Sección: Por canal
    writer.writerow(["=== LEADS POR CANAL ==="])
    writer.writerow(["Canal", "Cantidad", "Porcentaje"])

    total = metrics_data["total_leads"]
    for canal, count in sorted(metrics_data["leads_by_channel"].items(), key=lambda x: -x[1]):
        pct = (count / total * 100) if total > 0 else 0
        writer.writerow([canal, count, f"{pct:.1f}%"])

    writer.writerow([])

    # Sección: Por día
    writer.writerow(["=== LEADS POR DÍA ==="])
    writer.writerow(["Fecha", "Cantidad"])

    for day, count in metrics_data["leads_by_day"].items():
        writer.writerow([day, count])

    writer.writerow([])

    # Sección: Contactos por canal (con todas las columnas)
    writer.writerow(["=== DETALLE DE CONTACTOS POR CANAL ==="])
    contacts_by_channel = metrics_data.get("_contacts_by_channel", {})

    for canal in sorted(contacts_by_channel.keys()):
        contactos = contacts_by_channel[canal]
        writer.writerow([])
        writer.writerow([f"--- {canal.upper()} ({len(contactos)} leads) ---"])
        writer.writerow(["Fecha", "Canal", "Nombre", "Teléfono", "Motivo", "Status"])

        for contacto in contactos:
            writer.writerow([
                format_date_excel(contacto.get("fecha", "")),
                contacto.get("canal", canal.capitalize()),
                contacto.get("nombre", "Sin nombre"),
                contacto.get("telefono", "Sin teléfono"),
                contacto.get("motivo", "Consulta general"),
                contacto.get("status", "Lead"),
            ])

    # Generar nombre de archivo
    from datetime import datetime
    filename = f"metricas_redes_{days}d_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


async def export_metrics_excel(
    days: int = Query(7, ge=1, le=30, description="Días a analizar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Exporta métricas de redes sociales a formato Excel profesional.

    Genera un archivo .xlsx con:
    - Hoja "Resumen": Métricas agregadas por canal
    - Hoja "Contactos": Detalle de todos los leads con formato profesional
    - Hojas por canal: Si hay >5 contactos por canal
    """
    from fastapi.responses import Response
    import pandas as pd

    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        # Obtener datos de métricas
        metrics_data = await get_social_media_metrics(days=days, x_api_key=x_api_key)

        # Crear buffer en memoria para el archivo Excel
        output = BytesIO()

        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            workbook = writer.book

            # ========== FORMATOS ==========
            header_format = workbook.add_format({
                'bold': True,
                'font_color': 'white',
                'bg_color': '#1F4E79',  # Azul marino
                'border': 1,
                'align': 'center',
                'valign': 'vcenter',
                'text_wrap': True
            })

            title_format = workbook.add_format({
                'bold': True,
                'font_size': 14,
                'font_color': 'white',
                'bg_color': '#1F4E79',
                'align': 'center',
                'valign': 'vcenter',
            })

            # ========== HOJA 1: RESUMEN GENERAL ==========
            summary_data = {
                'Métrica': [
                    'Período Analizado',
                    'Total Leads',
                    'Instagram',
                    'Facebook',
                    'TikTok',
                    'LinkedIn',
                    'YouTube'
                ],
                'Valor': [
                    f"Últimos {days} días",
                    metrics_data['total_leads'],
                    metrics_data['leads_by_channel'].get('instagram', 0),
                    metrics_data['leads_by_channel'].get('facebook', 0),
                    metrics_data['leads_by_channel'].get('tiktok', 0),
                    metrics_data['leads_by_channel'].get('linkedin', 0),
                    metrics_data['leads_by_channel'].get('youtube', 0),
                ]
            }
            df_summary = pd.DataFrame(summary_data)
            df_summary.to_excel(writer, sheet_name='Resumen', index=False, startrow=1)

            ws_summary = writer.sheets['Resumen']
            ws_summary.merge_range('A1:B1', 'RESUMEN DE MÉTRICAS - REDES SOCIALES', title_format)
            ws_summary.set_column('A:A', 25)
            ws_summary.set_column('B:B', 20)
            ws_summary.freeze_panes(2, 0)

            # Aplicar formato a encabezados de resumen
            for col_num, col_name in enumerate(df_summary.columns):
                ws_summary.write(1, col_num, col_name, header_format)

            # ========== HOJA 2: DETALLE DE CONTACTOS ==========
            all_contacts = []
            contacts_by_channel = metrics_data.get('_contacts_by_channel', {})

            for canal, contactos in contacts_by_channel.items():
                for c in contactos:
                    # Los datos ya vienen sanitizados desde get_social_media_metrics
                    all_contacts.append({
                        'Fecha Registro': format_date_excel(c.get('fecha', '')),
                        'Canal': c.get('canal', canal.capitalize()),
                        'Nombre': c.get('nombre', 'Sin nombre'),
                        'Teléfono': c.get('telefono', 'Sin teléfono'),
                        'Motivo': c.get('motivo', 'Consulta general'),
                        'Status': c.get('status', 'Lead'),
                        'Score': c.get('score', '-'),
                    })

            if all_contacts:
                df_contacts = pd.DataFrame(all_contacts)

                # Ordenar columnas según requerimiento
                cols_order = ['Fecha Registro', 'Canal', 'Nombre', 'Teléfono', 'Motivo', 'Status', 'Score']
                df_contacts = df_contacts.reindex(columns=cols_order)

                df_contacts.to_excel(writer, sheet_name='Contactos', index=False, startrow=0)

                ws_contacts = writer.sheets['Contactos']

                # Aplicar formato a encabezados
                for col_num, col_name in enumerate(df_contacts.columns):
                    ws_contacts.write(0, col_num, col_name, header_format)

                # Auto-ajustar columnas
                for col_num, col_name in enumerate(df_contacts.columns):
                    try:
                        max_len = max(
                            df_contacts[col_name].astype(str).map(len).max(),
                            len(col_name)
                        ) + 2
                        ws_contacts.set_column(col_num, col_num, min(max_len, 40))
                    except Exception:
                        ws_contacts.set_column(col_num, col_num, 15)

                # Freeze pane y auto-filter
                ws_contacts.freeze_panes(1, 0)
                ws_contacts.autofilter(0, 0, len(df_contacts), len(df_contacts.columns) - 1)

            # ========== HOJAS POR CANAL (si >5 contactos) ==========
            for canal, contactos in contacts_by_channel.items():
                if len(contactos) > 5:
                    canal_data = []
                    for c in contactos:
                        canal_data.append({
                            'Fecha': format_date_excel(c.get('fecha', '')),
                            'Nombre': c.get('nombre', 'Sin nombre'),
                            'Teléfono': c.get('telefono', 'Sin teléfono'),
                            'Motivo': c.get('motivo', 'Consulta general'),
                            'Status': c.get('status', 'Lead'),
                            'Score': c.get('score', '-'),
                        })

                    df_canal = pd.DataFrame(canal_data)
                    sheet_name = canal.capitalize()[:31]  # Excel limita a 31 chars
                    df_canal.to_excel(writer, sheet_name=sheet_name, index=False)

                    ws_canal = writer.sheets[sheet_name]
                    for col_num, col_name in enumerate(df_canal.columns):
                        ws_canal.write(0, col_num, col_name, header_format)
                        ws_canal.set_column(col_num, col_num, 20)
                    ws_canal.freeze_panes(1, 0)
                    ws_canal.autofilter(0, 0, len(df_canal), len(df_canal.columns) - 1)

        # Preparar respuesta
        from urllib.parse import quote as _url_quote
        output.seek(0)
        filename = f"metricas_redes_{days}d_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        content_disposition = (
            f'attachment; filename="{filename}"; '
            f"filename*=UTF-8''{_url_quote(filename)}"
        )

        return Response(
            content=output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": content_disposition}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Metrics] Error exportando Excel: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generando Excel: {str(e)}")


# ============================================================================
# MÉTRICAS DE CITAS REALIZADAS (dashboard mercadeo)
# ============================================================================

async def get_appointments_metrics(
    date_from: str = Query(..., description="YYYY-MM-DD (Bogotá)"),
    date_to: str = Query(..., description="YYYY-MM-DD (Bogotá)"),
    worker_id: Optional[str] = Query(None, description="ID del trabajador de campo"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Métricas de citas completadas — para dashboard mercadeo.
    Filtra por status='completed' y visit_completed_at en rango.
    Segmenta por worker_id (trabajador de campo que realizó la visita).
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        from zoneinfo import ZoneInfo
        from collections import defaultdict
        TZ = ZoneInfo("America/Bogota")

        try:
            from_dt = datetime.fromisoformat(date_from).replace(tzinfo=TZ)
            to_dt = (datetime.fromisoformat(date_to).replace(tzinfo=TZ)
                     + timedelta(days=1))
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usa YYYY-MM-DD")

        if from_dt >= to_dt:
            raise HTTPException(status_code=400, detail="date_from debe ser < date_to")

        period_days = (to_dt - from_dt).days
        if period_days > 90:
            raise HTTPException(status_code=400, detail="Rango máximo: 90 días")

        mongo = get_mongo_manager()
        appointments = await mongo.get_completed_appointments(
            from_dt, to_dt, worker_id
        )

        total = len(appointments)
        by_day: Dict[str, int] = defaultdict(int)
        by_worker: Dict[str, int] = defaultdict(int)
        details = []
        now_bog = datetime.now(TZ)
        week_threshold = now_bog - timedelta(days=7)
        week_count = 0

        for apt in appointments:
            completed_at = apt.get("visit_completed_at")
            if completed_at is None:
                continue
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=ZoneInfo("UTC"))
            dt_bog = completed_at.astimezone(TZ)
            day_key = dt_bog.strftime("%Y-%m-%d")
            by_day[day_key] += 1

            if dt_bog >= week_threshold:
                week_count += 1

            worker_name = apt.get("worker_name") or "Sin asignar"
            by_worker[worker_name] += 1

            apt_dt = apt.get("appointment_dt")
            if apt_dt and apt_dt.tzinfo is None:
                apt_dt = apt_dt.replace(tzinfo=ZoneInfo("UTC"))

            details.append({
                "fecha_cita": apt_dt.astimezone(TZ).isoformat() if apt_dt else "",
                "nombre": apt.get("contact_name") or "Sin nombre",
                "telefono": apt.get("phone", ""),
                "asesor": worker_name,
                "canal": apt.get("canal") or "whatsapp",
            })

        # Delta vs período anterior
        prev_from = from_dt - timedelta(days=period_days)
        prev_to = from_dt
        prev_count = await mongo.count_completed_appointments(
            prev_from, prev_to, worker_id
        )
        if prev_count > 0:
            delta_pct = round((total - prev_count) / prev_count * 100, 1)
        else:
            delta_pct = None

        avg_per_day = round(total / period_days, 1) if period_days else 0

        return {
            "period": {"from": date_from, "to": date_to, "days": period_days},
            "total": total,
            "delta_pct": delta_pct,
            "week_count": week_count,
            "avg_per_day": avg_per_day,
            "by_day": dict(sorted(by_day.items())),
            "by_worker": dict(by_worker),
            "_details": details,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AppointmentsMetrics] Error: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def export_appointments_excel(
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    worker_id: Optional[str] = Query(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Exporta citas completadas a Excel para el área de mercadeo.
    3 hojas: Resumen, Citas (5 columnas), Por Trabajador (si >5 citas).
    """
    from fastapi.responses import Response
    from urllib.parse import quote as _url_quote
    import pandas as pd

    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        metrics_data = await get_appointments_metrics(
            date_from=date_from,
            date_to=date_to,
            worker_id=worker_id,
            x_api_key=x_api_key,
        )

        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            workbook = writer.book
            header_format = workbook.add_format({
                'bold': True, 'font_color': 'white', 'bg_color': '#1F4E79',
                'border': 1, 'align': 'center', 'valign': 'vcenter',
                'text_wrap': True,
            })
            title_format = workbook.add_format({
                'bold': True, 'font_size': 14, 'font_color': 'white',
                'bg_color': '#1F4E79', 'align': 'center', 'valign': 'vcenter',
            })

            # Hoja 1: Resumen
            delta_display = (
                f"{metrics_data['delta_pct']:+.1f}%"
                if metrics_data.get('delta_pct') is not None else "N/A"
            )
            summary_data = {
                'Métrica': [
                    'Período Analizado',
                    'Total Citas Realizadas',
                    'Esta Semana',
                    'Promedio por Día',
                    'Δ vs. Período Anterior',
                ],
                'Valor': [
                    f"{date_from} a {date_to}",
                    metrics_data['total'],
                    metrics_data['week_count'],
                    metrics_data['avg_per_day'],
                    delta_display,
                ],
            }
            df_summary = pd.DataFrame(summary_data)
            df_summary.to_excel(writer, sheet_name='Resumen', index=False, startrow=1)
            ws_summary = writer.sheets['Resumen']
            ws_summary.merge_range(
                'A1:B1',
                'RESUMEN DE CITAS REALIZADAS',
                title_format,
            )
            ws_summary.set_column('A:A', 28)
            ws_summary.set_column('B:B', 25)
            ws_summary.freeze_panes(2, 0)
            for col_num, col_name in enumerate(df_summary.columns):
                ws_summary.write(1, col_num, col_name, header_format)

            # Hoja 2: Citas (4 columnas)
            details = metrics_data.get('_details', [])
            if details:
                rows = [{
                    'Fecha Cita': _format_datetime_bogota(d.get('fecha_cita', '')),
                    'Nombre': d.get('nombre', 'Sin nombre'),
                    'Teléfono': d.get('telefono', ''),
                    'Asesor': d.get('asesor', 'Sin asignar'),
                } for d in details]
                df_citas = pd.DataFrame(rows)
                df_citas.to_excel(writer, sheet_name='Citas', index=False)
                ws_citas = writer.sheets['Citas']
                for col_num, col_name in enumerate(df_citas.columns):
                    ws_citas.write(0, col_num, col_name, header_format)
                    try:
                        max_len = max(
                            df_citas[col_name].astype(str).map(len).max(),
                            len(col_name),
                        ) + 2
                        ws_citas.set_column(col_num, col_num, min(max_len, 40))
                    except Exception:
                        ws_citas.set_column(col_num, col_num, 18)
                ws_citas.freeze_panes(1, 0)
                ws_citas.autofilter(0, 0, len(df_citas), len(df_citas.columns) - 1)

                # Hojas por asesor si >5 citas
                # Hojas por trabajador de campo (si >5 citas)
                by_worker_groups: Dict[str, list] = {}
                for row in rows:
                    by_worker_groups.setdefault(row['Asesor'], []).append(row)
                for worker_name, worker_rows in by_worker_groups.items():
                    if len(worker_rows) > 5:
                        sheet_name = (worker_name or "Sin asignar")[:31]
                        df_worker = pd.DataFrame(worker_rows)
                        df_worker.to_excel(writer, sheet_name=sheet_name, index=False)
                        ws_worker = writer.sheets[sheet_name]
                        for col_num, col_name in enumerate(df_worker.columns):
                            ws_worker.write(0, col_num, col_name, header_format)
                            ws_worker.set_column(col_num, col_num, 22)
                        ws_worker.freeze_panes(1, 0)
                        ws_worker.autofilter(
                            0, 0, len(df_worker), len(df_worker.columns) - 1
                        )

        output.seek(0)
        filename = (
            f"citas_realizadas_{date_from}_a_{date_to}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
        content_disposition = (
            f'attachment; filename="{filename}"; '
            f"filename*=UTF-8''{_url_quote(filename)}"
        )
        return Response(
            content=output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": content_disposition},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AppointmentsMetrics] Error export Excel: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _format_datetime_bogota(iso_str: str) -> str:
    """Formatea ISO datetime a 'DD/MM/YYYY HH:mm' en Bogotá."""
    if not iso_str:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso_str[:16]


async def metrics_dashboard_ui(request: Request, x_api_key: str = Query(None, alias="key")):
    """
    Dashboard de metricas para analista de redes sociales.

    Acceso: /whatsapp/panel/metrics/?key=TU_API_KEY
    """
    # Validar API Key via query param para acceso web
    if not _legacy._validate_api_key(x_api_key):
        return HTMLResponse(
            content="""<!DOCTYPE html>
<html lang="es">
<head><title>Acceso Denegado</title></head>
<body style="font-family: Arial; padding: 50px; text-align: center;">
    <h1>Acceso Denegado</h1>
    <p>Se requiere API Key válida.</p>
    <p>Uso: /whatsapp/panel/metrics/?key=TU_API_KEY</p>
</body>
</html>""",
            status_code=401
        )

    return templates.TemplateResponse(request, "metrics.html", {
        "api_key": x_api_key,
        "base_url": "/whatsapp/panel"
    })
