#!/usr/bin/env python3
"""
SCRIPT DE LIMPIEZA TOTAL - Sistema Sofía
==========================================

Este script realiza una limpieza completa de todas las bases de datos
para preparar el sistema para la migración de Leadsales → Sistema Sofía.

BASES DE DATOS AFECTADAS:
- Redis: Estados de conversación, metadatos, índices del panel
- MongoDB: Mensajes, contactos cache, citas, historial
- HubSpot: Contactos y deals creados por el chatbot (OPCIONAL)

⚠️  ADVERTENCIA: Este script es DESTRUCTIVO e IRREVERSIBLE.
    Use con extrema precaución y solo en entornos de prueba/staging
    o cuando esté 100% seguro de querer limpiar producción.

USO:
    python scripts/cleanup_all_data.py --dry-run          # Muestra qué se borraría
    python scripts/cleanup_all_data.py --redis            # Solo limpia Redis
    python scripts/cleanup_all_data.py --mongodb          # Solo limpia MongoDB
    python scripts/cleanup_all_data.py --hubspot          # Solo limpia HubSpot
    python scripts/cleanup_all_data.py --all              # Limpia TODO
    python scripts/cleanup_all_data.py --all --force      # Sin confirmación (PELIGROSO)

Autor: Sistema Sofía
Fecha: Marzo 2026
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime
from typing import Optional

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


# ============================================================================
# COLORES PARA TERMINAL
# ============================================================================
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    RESET = '\033[0m'


def print_header(text: str):
    print(f"\n{Colors.CYAN}{Colors.BOLD}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.CYAN}{Colors.BOLD}{text.center(60)}{Colors.RESET}")
    print(f"{Colors.CYAN}{Colors.BOLD}{'=' * 60}{Colors.RESET}\n")


def print_warning(text: str):
    print(f"{Colors.YELLOW}⚠️  {text}{Colors.RESET}")


def print_error(text: str):
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")


def print_success(text: str):
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")


def print_info(text: str):
    print(f"{Colors.BLUE}ℹ️  {text}{Colors.RESET}")


def print_danger(text: str):
    print(f"{Colors.RED}{Colors.BOLD}🚨 {text}{Colors.RESET}")


# ============================================================================
# LIMPIEZA DE REDIS
# ============================================================================
async def cleanup_redis(dry_run: bool = False) -> dict:
    """
    Limpia todas las claves de Redis relacionadas con el sistema.
    
    Claves afectadas:
    - conv_state:* (estados de conversación)
    - conv_meta:* (metadatos de conversación)
    - session:* (sesiones del orquestador)
    - active_conversations_* (índices del panel)
    - last_client_msg:* (ventana 24h WhatsApp)
    - lead_creation_lock:* (locks de deduplicación)
    - panel_templates:* (templates guardados)
    - lead_assigner:* (índices round robin)
    """
    print_header("LIMPIEZA DE REDIS")
    
    results = {
        "keys_found": 0,
        "keys_deleted": 0,
        "patterns": {},
        "errors": []
    }
    
    try:
        import redis.asyncio as redis
        
        # Detectar entorno
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
        )
        
        if not redis_url:
            print_error("REDIS_URL no configurada")
            results["errors"].append("REDIS_URL no configurada")
            return results
        
        # Ocultar credenciales en log
        safe_url = redis_url.split("@")[-1] if "@" in redis_url else redis_url
        print_info(f"Conectando a Redis: {safe_url}")
        
        client = redis.from_url(redis_url, decode_responses=True)
        await client.ping()
        print_success("Conexión a Redis establecida")
        
        # Patrones a limpiar
        patterns = [
            "conv_state:*",
            "conv_meta:*",
            "session:*",
            "active_conversations_*",
            "last_client_msg:*",
            "lead_creation_lock:*",
            "panel_templates:*",
            "lead_assigner:*",
            "hubspot_cache:*",
            "contact_cache:*",
        ]
        
        for pattern in patterns:
            keys = []
            async for key in client.scan_iter(match=pattern, count=1000):
                keys.append(key)
            
            results["patterns"][pattern] = len(keys)
            results["keys_found"] += len(keys)
            
            if keys:
                print_info(f"  {pattern}: {len(keys)} claves encontradas")
                
                if not dry_run:
                    for key in keys:
                        await client.delete(key)
                    results["keys_deleted"] += len(keys)
        
        # Limpiar ZSETs específicos
        zsets_to_clear = [
            "active_conversations_sorted",
            "active_conversations_index",
            "bot_controlled_conversations"  # SET para contactos BOT_ACTIVE sin notificaciones
        ]
        
        for zset in zsets_to_clear:
            exists = await client.exists(zset)
            if exists:
                size = await client.zcard(zset) if await client.type(zset) == "zset" else 0
                if size == 0:
                    size = await client.scard(zset) if await client.type(zset) == "set" else 0
                
                print_info(f"  {zset}: {size} miembros")
                results["keys_found"] += 1
                
                if not dry_run:
                    await client.delete(zset)
                    results["keys_deleted"] += 1
        
        await client.aclose()
        
        if dry_run:
            print_warning(f"DRY RUN: Se encontraron {results['keys_found']} claves (no se borraron)")
        else:
            print_success(f"Redis limpiado: {results['keys_deleted']} claves eliminadas")
        
    except Exception as e:
        print_error(f"Error en limpieza de Redis: {e}")
        results["errors"].append(str(e))
    
    return results


# ============================================================================
# LIMPIEZA DE MONGODB
# ============================================================================
async def cleanup_mongodb(dry_run: bool = False) -> dict:
    """
    Limpia las colecciones de MongoDB del sistema.
    
    Colecciones afectadas:
    - messages (historial de conversaciones)
    - contacts (cache local de contactos)
    - appointments (citas agendadas)
    - appointment_workers (equipo de campo) - OPCIONAL, mantener estructura
    - panel_advisors (asesores) - OPCIONAL, mantener estructura
    """
    print_header("LIMPIEZA DE MONGODB")
    
    results = {
        "collections": {},
        "total_deleted": 0,
        "errors": []
    }
    
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        
        # Detectar entorno
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        mongo_url = os.getenv("MONGO_URL") if is_railway else (
            os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGO_URL")
        )
        
        if not mongo_url:
            print_error("MONGO_URL no configurada")
            results["errors"].append("MONGO_URL no configurada")
            return results
        
        print_info("Conectando a MongoDB...")
        client = AsyncIOMotorClient(mongo_url)
        db = client.inmobiliaria_chat
        
        # Verificar conexión
        await client.admin.command('ping')
        print_success("Conexión a MongoDB establecida")
        
        # Colecciones a limpiar completamente
        collections_to_clear = [
            "messages",
            "contacts",
            "appointments",
        ]
        
        for coll_name in collections_to_clear:
            try:
                coll = db[coll_name]
                count = await coll.count_documents({})
                results["collections"][coll_name] = count
                
                print_info(f"  {coll_name}: {count} documentos")
                
                if not dry_run and count > 0:
                    delete_result = await coll.delete_many({})
                    results["total_deleted"] += delete_result.deleted_count
                    
            except Exception as e:
                print_error(f"  Error en {coll_name}: {e}")
                results["errors"].append(f"{coll_name}: {e}")
        
        # Colecciones a preservar estructura (solo mostrar info)
        preserve_collections = ["appointment_workers", "panel_advisors"]
        
        for coll_name in preserve_collections:
            try:
                coll = db[coll_name]
                count = await coll.count_documents({})
                print_info(f"  {coll_name}: {count} documentos (PRESERVADO - config del sistema)")
            except Exception:
                pass
        
        client.close()
        
        if dry_run:
            total = sum(results["collections"].values())
            print_warning(f"DRY RUN: Se borrarían {total} documentos (no se borraron)")
        else:
            print_success(f"MongoDB limpiado: {results['total_deleted']} documentos eliminados")
        
    except Exception as e:
        print_error(f"Error en limpieza de MongoDB: {e}")
        results["errors"].append(str(e))
    
    return results


# ============================================================================
# LIMPIEZA DE HUBSPOT
# ============================================================================
async def cleanup_hubspot(dry_run: bool = False, batch_size: int = 100) -> dict:
    """
    Limpia contactos y deals de HubSpot creados por el chatbot.
    
    CRITERIOS DE IDENTIFICACIÓN:
    - Contactos con propiedad 'chatbot_timestamp' (creados por Sofía)
    - Contactos con propiedad 'canal_origen' que contenga valores del bot
    - Deals asociados a esos contactos
    
    ⚠️  ADVERTENCIA: Esta operación es MUY LENTA debido a rate limits de HubSpot.
        HubSpot permite ~100 requests/10 segundos.
    """
    print_header("LIMPIEZA DE HUBSPOT")
    
    results = {
        "contacts_found": 0,
        "contacts_deleted": 0,
        "deals_found": 0,
        "deals_deleted": 0,
        "errors": []
    }
    
    try:
        import httpx
        
        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
        if not hubspot_api_key:
            print_error("HUBSPOT_API_KEY no configurada")
            results["errors"].append("HUBSPOT_API_KEY no configurada")
            return results
        
        print_info("Conectando a HubSpot API...")
        
        headers = {
            "Authorization": f"Bearer {hubspot_api_key}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # ================================================================
            # PASO 1: Buscar contactos creados por el chatbot
            # ================================================================
            print_info("Buscando contactos creados por Sofía...")
            
            contacts_to_delete = []
            after = None
            
            # Buscar contactos con chatbot_timestamp (marca de Sofía)
            while True:
                search_url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
                search_body = {
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "chatbot_timestamp",
                                    "operator": "HAS_PROPERTY"
                                }
                            ]
                        }
                    ],
                    "properties": ["firstname", "lastname", "phone", "chatbot_timestamp"],
                    "limit": batch_size
                }
                
                if after:
                    search_body["after"] = after
                
                response = await client.post(search_url, json=search_body, headers=headers)
                
                if response.status_code == 429:
                    print_warning("Rate limit alcanzado, esperando 10 segundos...")
                    await asyncio.sleep(10)
                    continue
                
                if response.status_code != 200:
                    print_error(f"Error buscando contactos: {response.status_code}")
                    break
                
                data = response.json()
                contacts = data.get("results", [])
                
                for contact in contacts:
                    contact_id = contact["id"]
                    props = contact.get("properties", {})
                    name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "Sin nombre"
                    phone = props.get("phone", "Sin teléfono")
                    contacts_to_delete.append({
                        "id": contact_id,
                        "name": name,
                        "phone": phone
                    })
                
                # Paginación
                paging = data.get("paging", {})
                after = paging.get("next", {}).get("after")
                
                if not after or not contacts:
                    break
                
                # Pequeña pausa para no saturar la API
                await asyncio.sleep(0.5)
            
            results["contacts_found"] = len(contacts_to_delete)
            print_info(f"  Encontrados: {results['contacts_found']} contactos del chatbot")
            
            if contacts_to_delete and not dry_run:
                print_info("Eliminando contactos...")
                
                # Primero buscar y eliminar deals asociados
                for i, contact in enumerate(contacts_to_delete):
                    contact_id = contact["id"]
                    
                    # Buscar deals asociados
                    assoc_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}/associations/deals"
                    
                    try:
                        assoc_response = await client.get(assoc_url, headers=headers)
                        
                        if assoc_response.status_code == 200:
                            deals = assoc_response.json().get("results", [])
                            
                            for deal in deals:
                                deal_id = deal["id"]
                                results["deals_found"] += 1
                                
                                # Eliminar deal
                                delete_url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
                                del_response = await client.delete(delete_url, headers=headers)
                                
                                if del_response.status_code in [200, 204]:
                                    results["deals_deleted"] += 1
                                elif del_response.status_code == 429:
                                    await asyncio.sleep(10)
                        
                    except Exception as e:
                        results["errors"].append(f"Error con deals de {contact_id}: {e}")
                    
                    # Eliminar contacto
                    delete_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
                    
                    try:
                        del_response = await client.delete(delete_url, headers=headers)
                        
                        if del_response.status_code in [200, 204]:
                            results["contacts_deleted"] += 1
                        elif del_response.status_code == 429:
                            print_warning("Rate limit, esperando...")
                            await asyncio.sleep(10)
                            # Reintentar
                            del_response = await client.delete(delete_url, headers=headers)
                            if del_response.status_code in [200, 204]:
                                results["contacts_deleted"] += 1
                    
                    except Exception as e:
                        results["errors"].append(f"Error eliminando {contact_id}: {e}")
                    
                    # Progreso cada 10 contactos
                    if (i + 1) % 10 == 0:
                        print_info(f"  Progreso: {i + 1}/{len(contacts_to_delete)} contactos procesados")
                    
                    # Pausa obligatoria para rate limits
                    await asyncio.sleep(0.2)
        
        if dry_run:
            print_warning(f"DRY RUN: Se eliminarían {results['contacts_found']} contactos y sus deals")
        else:
            print_success(
                f"HubSpot limpiado: {results['contacts_deleted']} contactos, "
                f"{results['deals_deleted']} deals eliminados"
            )
        
    except Exception as e:
        print_error(f"Error en limpieza de HubSpot: {e}")
        results["errors"].append(str(e))
    
    return results


# ============================================================================
# FUNCIÓN PRINCIPAL
# ============================================================================
async def main():
    parser = argparse.ArgumentParser(
        description="Script de limpieza total del Sistema Sofía",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python cleanup_all_data.py --dry-run          Ver qué se borraría sin borrar nada
  python cleanup_all_data.py --redis            Solo limpiar Redis
  python cleanup_all_data.py --mongodb          Solo limpiar MongoDB
  python cleanup_all_data.py --hubspot          Solo limpiar HubSpot (LENTO)
  python cleanup_all_data.py --all              Limpiar TODO
  python cleanup_all_data.py --all --force      Sin confirmación (¡PELIGROSO!)
        """
    )
    
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra qué se borraría, sin borrar nada")
    parser.add_argument("--redis", action="store_true",
                        help="Limpiar Redis (estados y cache)")
    parser.add_argument("--mongodb", action="store_true",
                        help="Limpiar MongoDB (mensajes y citas)")
    parser.add_argument("--hubspot", action="store_true",
                        help="Limpiar HubSpot (contactos y deals del chatbot)")
    parser.add_argument("--all", action="store_true",
                        help="Limpiar TODAS las bases de datos")
    parser.add_argument("--force", action="store_true",
                        help="No pedir confirmación (¡PELIGROSO!)")
    
    args = parser.parse_args()
    
    # Si no se especifica ninguna opción, mostrar ayuda
    if not any([args.redis, args.mongodb, args.hubspot, args.all, args.dry_run]):
        parser.print_help()
        return
    
    # Banner
    print_header("LIMPIEZA TOTAL - SISTEMA SOFÍA")
    print_info(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print_info(f"Entorno: {'Railway' if os.getenv('RAILWAY_ENVIRONMENT') else 'Local'}")
    
    if args.dry_run:
        print_warning("MODO DRY-RUN: No se borrará nada, solo se mostrará información")
    
    # Determinar qué limpiar
    clean_redis = args.redis or args.all
    clean_mongodb = args.mongodb or args.all
    clean_hubspot = args.hubspot or args.all
    
    # Mostrar resumen de lo que se va a hacer
    print("\n" + Colors.BOLD + "Se limpiarán las siguientes bases de datos:" + Colors.RESET)
    if clean_redis:
        print(f"  {Colors.GREEN}✓{Colors.RESET} Redis (estados, cache, índices del panel)")
    if clean_mongodb:
        print(f"  {Colors.GREEN}✓{Colors.RESET} MongoDB (mensajes, contactos cache, citas)")
    if clean_hubspot:
        print(f"  {Colors.GREEN}✓{Colors.RESET} HubSpot (contactos y deals del chatbot)")
    
    # Confirmación (a menos que sea --force o --dry-run)
    if not args.force and not args.dry_run:
        print_danger("\n¡ADVERTENCIA! Esta operación es IRREVERSIBLE.")
        print_danger("Se eliminarán TODOS los datos de las bases seleccionadas.")
        
        confirm = input(f"\n{Colors.YELLOW}¿Está seguro de continuar? Escriba 'SI BORRAR' para confirmar: {Colors.RESET}")
        
        if confirm.strip() != "SI BORRAR":
            print_info("Operación cancelada por el usuario")
            return
    
    # Ejecutar limpieza
    results = {}
    
    if clean_redis:
        results["redis"] = await cleanup_redis(dry_run=args.dry_run)
    
    if clean_mongodb:
        results["mongodb"] = await cleanup_mongodb(dry_run=args.dry_run)
    
    if clean_hubspot:
        print_warning("\nLa limpieza de HubSpot puede tardar varios minutos debido a rate limits...")
        results["hubspot"] = await cleanup_hubspot(dry_run=args.dry_run)
    
    # Resumen final
    print_header("RESUMEN FINAL")
    
    if "redis" in results:
        r = results["redis"]
        print(f"Redis: {r.get('keys_deleted', 0)} claves eliminadas de {r.get('keys_found', 0)} encontradas")
        if r.get("errors"):
            print_error(f"  Errores: {', '.join(r['errors'])}")
    
    if "mongodb" in results:
        m = results["mongodb"]
        print(f"MongoDB: {m.get('total_deleted', 0)} documentos eliminados")
        if m.get("errors"):
            print_error(f"  Errores: {', '.join(m['errors'])}")
    
    if "hubspot" in results:
        h = results["hubspot"]
        print(f"HubSpot: {h.get('contacts_deleted', 0)}/{h.get('contacts_found', 0)} contactos, "
              f"{h.get('deals_deleted', 0)}/{h.get('deals_found', 0)} deals eliminados")
        if h.get("errors"):
            print_error(f"  Errores: {len(h['errors'])} errores encontrados")
    
    if args.dry_run:
        print_warning("\nMODO DRY-RUN: No se eliminó nada. Use sin --dry-run para ejecutar.")
    else:
        print_success("\n¡Limpieza completada!")
        print_info("El sistema está listo para recibir contactos de la migración Leadsales → Sofía")


if __name__ == "__main__":
    asyncio.run(main())
