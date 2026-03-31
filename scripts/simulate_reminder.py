"""
Bug 3 — Naming: Simula check_appointment_reminders() y check_appointment_followups()
SIN enviar mensajes por Twilio. Imprime el mensaje exacto que se enviaría a cada contacto.

Útil para verificar localmente que el nombre se resuelve correctamente antes de
que el scheduler real se dispare.

Uso:
    python scripts/simulate_reminder.py              # simula recordatorios + followups
    python scripts/simulate_reminder.py reminders    # solo recordatorios
    python scripts/simulate_reminder.py followups    # solo seguimientos
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fix encoding en Windows (cp1252 no soporta caracteres Unicode)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"

REDIS_URL = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))


async def resolve_name(apt, state_manager) -> tuple[str, str]:
    """
    Replica exacta de la lógica implementada en app.py.
    Retorna (contact_name, source).
    """
    contact_name = apt.contact_name
    source = "appointment_redis"

    if not contact_name:
        try:
            meta = await state_manager.get_meta(apt.phone_normalized, apt.canal)
            if meta and meta.display_name:
                contact_name = meta.display_name.split()[0]
                source = "redis_meta"
        except Exception as e:
            print(f"  {YELLOW}⚠ Error obteniendo meta Redis: {e}{RESET}")

    if not contact_name:
        contact_name = "cliente"
        source = "fallback"

    return contact_name, source


async def simulate_reminders(apt_manager, state_manager):
    print(f"\n{BOLD}{BLUE}══ RECORDATORIOS (1h antes de la cita) ══{RESET}")

    appointments = await apt_manager.get_appointments_needing_reminder()
    print(f"Citas pendientes de recordatorio: {len(appointments)}\n")

    if not appointments:
        print(f"  {YELLOW}(ninguna cita requiere recordatorio ahora){RESET}")
        return

    for apt in appointments:
        contact_name, source = await resolve_name(apt, state_manager)

        scheduled_dt = apt.scheduled_dt
        hora = scheduled_dt.strftime('%H:%M')

        message = (
            f"¡Hola {contact_name}! 👋 Te recuerdo tu cita programada para hoy "
            f"a las {hora}. ¡Te esperamos!"
        )

        _ok = contact_name != "cliente"
        indicator = f"{GREEN}✓{RESET}" if _ok else f"{YELLOW}⚠{RESET}"

        print(f"  {indicator} {apt.phone_normalized}")
        print(f"     Nombre      : {GREEN if _ok else YELLOW}{contact_name!r} (source={source}){RESET}")
        print(f"     Cita        : {scheduled_dt.strftime('%Y-%m-%d %H:%M')}")
        print(f"     Estado cita : {apt.status.value}")
        print(f"     {BLUE}Mensaje:{RESET} {message}")
        print()


async def simulate_followups(apt_manager, state_manager):
    print(f"\n{BOLD}{BLUE}══ SEGUIMIENTOS (post-cita ~1h30m después) ══{RESET}")

    appointments = await apt_manager.get_appointments_needing_followup()
    print(f"Citas pendientes de seguimiento: {len(appointments)}\n")

    if not appointments:
        print(f"  {YELLOW}(ninguna cita requiere seguimiento ahora){RESET}")
        return

    for apt in appointments:
        contact_name, source = await resolve_name(apt, state_manager)

        message = (
            f"¡Hola {contact_name}! 😊 Esperamos que tu visita haya sido de tu agrado. "
            f"¿Tienes alguna pregunta sobre el inmueble que visitaste? "
            f"Estamos aquí para ayudarte."
        )

        _ok = contact_name != "cliente"
        indicator = f"{GREEN}✓{RESET}" if _ok else f"{YELLOW}⚠{RESET}"

        print(f"  {indicator} {apt.phone_normalized}")
        print(f"     Nombre      : {GREEN if _ok else YELLOW}{contact_name!r} (source={source}){RESET}")
        print(f"     Estado cita : {apt.status.value}")
        print(f"     {BLUE}Mensaje:{RESET} {message}")
        print()


async def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if mode not in ("all", "reminders", "followups"):
        print(f"Uso: python scripts/simulate_reminder.py [all|reminders|followups]")
        sys.exit(1)

    from middleware.appointment_manager import AppointmentManager
    from middleware.conversation_state import ConversationStateManager

    apt_manager   = AppointmentManager(REDIS_URL)
    state_manager = ConversationStateManager(REDIS_URL)

    print(f"\n{BOLD}=== Bug 3 Naming — Simulación de Mensajes ==={RESET}")
    print(f"Redis : {REDIS_URL.split('@')[-1]}")
    print(f"Modo  : {mode}")

    if mode in ("all", "reminders"):
        await simulate_reminders(apt_manager, state_manager)

    if mode in ("all", "followups"):
        await simulate_followups(apt_manager, state_manager)

    print(f"\n{BOLD}Leyenda:{RESET}")
    print(f"  {GREEN}✓{RESET} Nombre resuelto correctamente")
    print(f"  {YELLOW}⚠{RESET} Usando fallback 'cliente' (cita sin nombre o contacto sin meta en Redis)")

    print(f"\n{BOLD}Fuentes posibles (source):{RESET}")
    print(f"  appointment_redis — nombre guardado al crear la cita (fix activo)")
    print(f"  redis_meta        — nombre obtenido de ConversationMeta.display_name")
    print(f"  fallback          — ninguna fuente disponible, se usa 'cliente'")

    await apt_manager.close()
    try:
        await state_manager.redis.aclose()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
