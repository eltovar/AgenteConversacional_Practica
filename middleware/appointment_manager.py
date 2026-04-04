# middleware/appointment_manager.py
"""
Gestor de Citas en Redis para el sistema de recordatorios automáticos.

OPTIMIZACIONES v2.0:
- Todas las comparaciones de tiempo usan ZoneInfo("America/Bogota")
- Timestamps se guardan siempre con offset explícito (-05:00)
- Conversión correcta desde UTC al recuperar de Redis
- Idempotencia con flags de notificación
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional, List
from enum import Enum
from zoneinfo import ZoneInfo

import redis.asyncio as redis

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES DE ZONA HORARIA
# ═══════════════════════════════════════════════════════════════════════════════

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")


def get_bogota_now() -> datetime:
    """Retorna datetime actual en zona horaria de Bogotá."""
    return datetime.now(TIMEZONE_BOGOTA)


def get_bogota_now_iso() -> str:
    """
    Retorna datetime actual en formato ISO con offset explícito.
    Ejemplo: 2024-02-23T10:30:00-05:00
    """
    return datetime.now(TIMEZONE_BOGOTA).isoformat()


def parse_datetime_to_bogota(dt_string: str) -> datetime:
    """
    Parsea un string de datetime y lo convierte a zona horaria de Bogotá.

    Maneja:
    - ISO format con 'Z': 2024-01-15T10:30:00Z → convierte a Bogotá
    - ISO format con offset: 2024-01-15T10:30:00-05:00 → mantiene
    - ISO format naive: 2024-01-15T10:30:00 → asume Bogotá
    """
    if not dt_string:
        return get_bogota_now()

    # Reemplazar Z por UTC offset
    dt_string = dt_string.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(dt_string)
        if dt.tzinfo is None:
            # Naive datetime: asumir Bogotá
            dt = dt.replace(tzinfo=TIMEZONE_BOGOTA)
        else:
            # Convertir a Bogotá para comparaciones consistentes
            dt = dt.astimezone(TIMEZONE_BOGOTA)
        return dt
    except (ValueError, TypeError) as e:
        logger.warning(
            "[AppointmentManager] Error parseando datetime '%s': %s",
            dt_string, e
        )
        return get_bogota_now()


def timestamp_to_bogota(ts: float) -> datetime:
    """
    Convierte un timestamp Unix a datetime en zona horaria de Bogotá.

    Args:
        ts: Timestamp Unix (segundos desde epoch)

    Returns:
        datetime en zona horaria de Bogotá
    """
    # Crear datetime en UTC y convertir a Bogotá
    from datetime import timezone as tz
    dt_utc = datetime.fromtimestamp(ts, tz=tz.utc)
    return dt_utc.astimezone(TIMEZONE_BOGOTA)


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS Y DATACLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class AppointmentStatus(str, Enum):
    """Estados posibles de una cita."""
    PENDING = "pending"           # Cita agendada, esperando
    CONFIRMED = "confirmed"       # Cliente confirmó asistencia
    COMPLETED = "completed"       # Cita realizada exitosamente
    CANCELLED = "cancelled"       # Cita cancelada
    NO_SHOW = "no_show"          # Cliente no asistió


@dataclass
class Appointment:
    """Modelo de datos para una cita."""
    phone_normalized: str
    canal: str
    scheduled_datetime: str       # ISO format con offset (ej: 2024-02-23T10:00:00-05:00)
    status: AppointmentStatus = AppointmentStatus.PENDING
    reminder_sent: bool = False
    followup_sent: bool = False
    created_at: Optional[str] = None
    contact_name: Optional[str] = None
    contact_id: Optional[str] = None
    notes: Optional[str] = None

    # NUEVOS: Para idempotencia de notificaciones
    reminder_sent_at: Optional[str] = None
    followup_sent_at: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Appointment":
        if "status" in data and isinstance(data["status"], str):
            try:
                data["status"] = AppointmentStatus(data["status"])
            except ValueError:
                data["status"] = AppointmentStatus.PENDING

        # Filtrar campos desconocidos para compatibilidad
        import dataclasses
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}

        return cls(**filtered_data)

    @property
    def scheduled_dt(self) -> datetime:
        """
        Retorna el datetime de la cita en zona horaria de Bogotá.
        CORREGIDO: Siempre retorna en TIMEZONE_BOGOTA.
        """
        return parse_datetime_to_bogota(self.scheduled_datetime)


# ═══════════════════════════════════════════════════════════════════════════════
# APPOINTMENT MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class AppointmentManager:
    """
    Gestor de citas en Redis.

    OPTIMIZACIONES v2.0:
    - Todas las operaciones de tiempo usan ZoneInfo("America/Bogota")
    - Timestamps en Redis se guardan con offset explícito
    - Logs detallados con diferencias de tiempo
    """

    APPOINTMENT_PREFIX = "appointment:"
    APPOINTMENT_INDEX = "appointment_index"  # Sorted set para búsquedas por tiempo

    # NUEVO: Prefijos para control de notificaciones
    NOTIFICATION_SENT_PREFIX = "apt_notif_sent:"

    # TTL de 30 días para citas
    APPOINTMENT_TTL = 30 * 24 * 60 * 60

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis: Optional[redis.Redis] = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self._redis

    async def close(self):
        """Cierra la conexión de Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    def _build_key(self, phone: str, canal: str) -> str:
        return f"{self.APPOINTMENT_PREFIX}{phone}:{canal}"

    def _build_notification_key(self, phone: str, canal: str, notif_type: str) -> str:
        """Construye key para tracking de notificaciones."""
        return f"{self.NOTIFICATION_SENT_PREFIX}{notif_type}:{phone}:{canal}"

    # ═══════════════════════════════════════════════════════════════════════════
    # CRUD DE CITAS
    # ═══════════════════════════════════════════════════════════════════════════

    async def create_appointment(
        self,
        phone_normalized: str,
        canal: str,
        scheduled_datetime: datetime,
        contact_name: Optional[str] = None,
        contact_id: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Appointment:
        """
        Crea una nueva cita.

        CORREGIDO: Guarda scheduled_datetime con offset explícito de Bogotá.
        """
        r = await self._get_redis()

        # Asegurar timezone de Bogotá
        if scheduled_datetime.tzinfo is None:
            scheduled_datetime = scheduled_datetime.replace(tzinfo=TIMEZONE_BOGOTA)
        else:
            # Convertir a Bogotá si viene en otro timezone
            scheduled_datetime = scheduled_datetime.astimezone(TIMEZONE_BOGOTA)

        appointment = Appointment(
            phone_normalized=phone_normalized,
            canal=canal,
            # CRÍTICO: Guardar con offset explícito (ej: 2024-02-23T10:00:00-05:00)
            scheduled_datetime=scheduled_datetime.isoformat(),
            created_at=get_bogota_now_iso(),
            contact_name=contact_name,
            contact_id=contact_id,
            notes=notes
        )

        key = self._build_key(phone_normalized, canal)

        # Guardar cita como JSON
        await r.set(key, json.dumps(appointment.to_dict()), ex=self.APPOINTMENT_TTL)

        # Agregar al índice ordenado por fecha
        # NOTA: El timestamp se guarda en UTC para que zrangebyscore funcione correctamente
        # pero la cita tiene el offset de Bogotá para display
        await r.zadd(
            self.APPOINTMENT_INDEX,
            {key: scheduled_datetime.timestamp()}
        )

        logger.info(
            "[AppointmentManager] Cita creada: %s:%s para %s (offset: %s)",
            phone_normalized, canal,
            scheduled_datetime.strftime('%Y-%m-%d %H:%M'),
            scheduled_datetime.strftime('%z')
        )

        return appointment

    async def get_appointment(
        self,
        phone_normalized: str,
        canal: str
    ) -> Optional[Appointment]:
        """Obtiene una cita existente."""
        r = await self._get_redis()
        key = self._build_key(phone_normalized, canal)

        data_str = await r.get(key)
        if not data_str:
            return None

        try:
            data = json.loads(data_str)
            return Appointment.from_dict(data)
        except Exception as e:
            logger.error("[AppointmentManager] Error deserializando cita: %s", e)
            return None

    async def update_appointment(self, appointment: Appointment) -> bool:
        """Actualiza una cita existente."""
        r = await self._get_redis()
        key = self._build_key(appointment.phone_normalized, appointment.canal)

        try:
            await r.set(
                key,
                json.dumps(appointment.to_dict()),
                ex=self.APPOINTMENT_TTL
            )
            return True
        except Exception as e:
            logger.error("[AppointmentManager] Error actualizando cita: %s", e)
            return False

    async def reschedule_appointment(
        self,
        phone_normalized: str,
        canal: str,
        new_scheduled_datetime: datetime
    ) -> bool:
        """
        Reprograma una cita: actualiza el score en ZSET y resetea flags de notificación.
        Llamar cuando PATCH /appointments cambia appointment_dt para que el scheduler
        use la nueva ventana de tiempo (recordatorio y seguimiento).
        """
        # Normalizar a TZ Bogotá
        if new_scheduled_datetime.tzinfo is None:
            new_scheduled_datetime = new_scheduled_datetime.replace(tzinfo=TIMEZONE_BOGOTA)
        else:
            new_scheduled_datetime = new_scheduled_datetime.astimezone(TIMEZONE_BOGOTA)

        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            logger.warning(
                "[AppointmentManager] reschedule: cita no encontrada en Redis %s:%s",
                phone_normalized, canal
            )
            return False

        # Actualizar datetime y resetear flags para que disparar en nueva ventana
        appointment.scheduled_datetime = new_scheduled_datetime.isoformat()
        appointment.reminder_sent = False
        appointment.reminder_sent_at = None
        appointment.followup_sent = False
        appointment.followup_sent_at = None

        r = await self._get_redis()
        key = self._build_key(phone_normalized, canal)

        # Persistir JSON actualizado
        await r.set(key, json.dumps(appointment.to_dict()), ex=self.APPOINTMENT_TTL)

        # Actualizar score en ZSET (este es el fix crítico)
        await r.zadd(self.APPOINTMENT_INDEX, {key: new_scheduled_datetime.timestamp()})

        # Limpiar keys de idempotencia para permitir nuevos envíos en hora correcta
        reminder_key = self._build_notification_key(phone_normalized, canal, "reminder")
        followup_key = self._build_notification_key(phone_normalized, canal, "followup")
        await r.delete(reminder_key, followup_key)

        logger.info(
            "[AppointmentManager] Cita reprogramada: %s:%s → %s",
            phone_normalized, canal,
            new_scheduled_datetime.strftime('%Y-%m-%d %H:%M %Z')
        )
        return True

    # ═══════════════════════════════════════════════════════════════════════════
    # MARCADO DE NOTIFICACIONES (CON IDEMPOTENCIA)
    # ═══════════════════════════════════════════════════════════════════════════

    async def mark_reminder_sent(self, phone_normalized: str, canal: str) -> bool:
        """
        Marca que se envió el recordatorio (24h antes).
        IDEMPOTENTE: Guarda timestamp de envío.
        """
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        now_iso = get_bogota_now_iso()
        appointment.reminder_sent = True
        appointment.reminder_sent_at = now_iso

        # También guardar en key separada para doble verificación
        r = await self._get_redis()
        notif_key = self._build_notification_key(phone_normalized, canal, "reminder")
        await r.set(notif_key, now_iso, ex=7 * 24 * 60 * 60)  # 7 días TTL

        return await self.update_appointment(appointment)

    async def mark_followup_sent(self, phone_normalized: str, canal: str) -> bool:
        """
        Marca que se envió el seguimiento post-cita (24h después).
        IDEMPOTENTE: Guarda timestamp de envío.
        """
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        now_iso = get_bogota_now_iso()
        appointment.followup_sent = True
        appointment.followup_sent_at = now_iso

        # También guardar en key separada
        r = await self._get_redis()
        notif_key = self._build_notification_key(phone_normalized, canal, "followup")
        await r.set(notif_key, now_iso, ex=7 * 24 * 60 * 60)

        return await self.update_appointment(appointment)

    async def was_notification_sent(
        self,
        phone_normalized: str,
        canal: str,
        notif_type: str
    ) -> bool:
        """
        Verifica si ya se envió una notificación.
        Útil para prevenir duplicados tras reinicio del servidor.
        """
        r = await self._get_redis()
        notif_key = self._build_notification_key(phone_normalized, canal, notif_type)
        return await r.exists(notif_key) > 0

    # ═══════════════════════════════════════════════════════════════════════════
    # OPERACIONES DE ESTADO
    # ═══════════════════════════════════════════════════════════════════════════

    async def confirm_appointment(self, phone_normalized: str, canal: str) -> bool:
        """Confirma una cita (cliente confirmó asistencia)."""
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        appointment.status = AppointmentStatus.CONFIRMED
        return await self.update_appointment(appointment)

    async def complete_appointment(self, phone_normalized: str, canal: str) -> bool:
        """Marca una cita como completada (visita realizada)."""
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        appointment.status = AppointmentStatus.COMPLETED
        success = await self.update_appointment(appointment)

        if success:
            logger.info(
                "[AppointmentManager] Cita completada: %s:%s",
                phone_normalized, canal
            )

        return success

    async def cancel_appointment(self, phone_normalized: str, canal: str) -> bool:
        """Cancela una cita."""
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        appointment.status = AppointmentStatus.CANCELLED
        success = await self.update_appointment(appointment)

        if success:
            # Remover del índice
            r = await self._get_redis()
            key = self._build_key(phone_normalized, canal)
            await r.zrem(self.APPOINTMENT_INDEX, key)
            # Limpiar keys de idempotencia — evita que una nueva cita del mismo
            # cliente herede el flag "recordatorio ya enviado" de la cita cancelada
            reminder_key = self._build_notification_key(phone_normalized, canal, "reminder")
            followup_key = self._build_notification_key(phone_normalized, canal, "followup")
            await r.delete(reminder_key, followup_key)
            logger.info(
                "[AppointmentManager] Cita cancelada: %s:%s",
                phone_normalized, canal
            )

        return success

    # ═══════════════════════════════════════════════════════════════════════════
    # BÚSQUEDA DE CITAS PARA NOTIFICACIONES
    # ═══════════════════════════════════════════════════════════════════════════

    async def get_appointments_needing_reminder(self) -> List[Appointment]:
        """
        Obtiene citas que necesitan recordatorio (5h antes).

        CORREGIDO:
        - Usa datetime.now(TIMEZONE_BOGOTA) para comparaciones
        - El score en Redis está en UTC, pero la comparación es correcta
          porque timestamp() siempre retorna segundos desde epoch UTC

        Returns:
            Lista de Appointments que necesitan recordatorio
        """
        r = await self._get_redis()

        # CRÍTICO: Usar timezone de Bogotá para "ahora"
        now = get_bogota_now()

        # Buscar citas programadas entre 4.5h y 5.5h desde ahora
        # (ventana de 1h para el job que corre cada 30min)
        min_time = now + timedelta(hours=4, minutes=30)
        max_time = now + timedelta(hours=5, minutes=30)

        logger.info(
            "[AppointmentManager] Buscando recordatorios: ahora=%s, "
            "ventana=[%s - %s] (Bogotá)",
            now.strftime('%Y-%m-%d %H:%M %Z'),
            min_time.strftime('%H:%M'),
            max_time.strftime('%H:%M')
        )

        # Obtener keys del índice usando timestamps UTC
        keys = await r.zrangebyscore(
            self.APPOINTMENT_INDEX,
            min_time.timestamp(),
            max_time.timestamp()
        )

        appointments = []
        for key in keys:
            data_str = await r.get(key)
            if data_str:
                try:
                    data = json.loads(data_str)
                    apt = Appointment.from_dict(data)

                    # Extraer phone y canal para verificación de idempotencia
                    parts = key.replace(self.APPOINTMENT_PREFIX, "").rsplit(":", 1)
                    phone = parts[0] if parts else apt.phone_normalized
                    canal = parts[1] if len(parts) > 1 else apt.canal

                    # IDEMPOTENCIA: Verificar si ya se envió (doble check)
                    if await self.was_notification_sent(phone, canal, "reminder"):
                        logger.debug(
                            "[AppointmentManager] Recordatorio ya enviado para %s:%s, saltando",
                            phone, canal
                        )
                        continue

                    # Solo incluir si:
                    # - No se ha enviado recordatorio
                    # - Estado es pending o confirmed
                    if (not apt.reminder_sent and
                            apt.status in [AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]):

                        # Log detallado
                        apt_dt = apt.scheduled_dt
                        hours_until = (apt_dt - now).total_seconds() / 3600

                        logger.info(
                            "[Scheduler] Verificando cita %s: "
                            "Diferencia de tiempo %.1f horas, Estado %s",
                            phone, hours_until, apt.status.value
                        )

                        appointments.append(apt)

                except Exception as e:
                    logger.error(
                        "[AppointmentManager] Error procesando cita %s: %s",
                        key, e
                    )

        logger.info(
            "[AppointmentManager] Citas necesitando recordatorio: %d",
            len(appointments)
        )
        return appointments

    async def get_appointments_needing_followup(self) -> List[Appointment]:
        """
        Obtiene citas que necesitan seguimiento post-visita.

        Ventana: citas que ocurrieron hace entre 1h15min y 1h45min
        (punto medio = 1h30min después de la cita).
        Ej: cita a las 3pm → mensaje se envía entre 4:15pm y 4:45pm.

        Returns:
            Lista de Appointments que necesitan followup
        """
        r = await self._get_redis()

        # CRÍTICO: Usar timezone de Bogotá
        now = get_bogota_now()

        # Ventana: 1h15min – 1h45min después de la cita
        min_time = now - timedelta(minutes=105)  # hace 1h45min
        max_time = now - timedelta(minutes=75)   # hace 1h15min

        logger.info(
            "[AppointmentManager] Buscando seguimientos post-visita: ahora=%s, "
            "ventana=[-1h45min a -1h15min] (Bogotá)",
            now.strftime('%Y-%m-%d %H:%M %Z')
        )

        keys = await r.zrangebyscore(
            self.APPOINTMENT_INDEX,
            min_time.timestamp(),
            max_time.timestamp()
        )

        appointments = []
        for key in keys:
            data_str = await r.get(key)
            if data_str:
                try:
                    data = json.loads(data_str)
                    apt = Appointment.from_dict(data)

                    # Extraer phone y canal
                    parts = key.replace(self.APPOINTMENT_PREFIX, "").rsplit(":", 1)
                    phone = parts[0] if parts else apt.phone_normalized
                    canal = parts[1] if len(parts) > 1 else apt.canal

                    # IDEMPOTENCIA: Verificar si ya se envió
                    if await self.was_notification_sent(phone, canal, "followup"):
                        logger.debug(
                            "[AppointmentManager] Seguimiento ya enviado para %s:%s, saltando",
                            phone, canal
                        )
                        continue

                    # Auto-completar citas que ya pasaron su horario y siguen en PENDING/CONFIRMED
                    if apt.status in (AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED):
                        prev_status = apt.status.value
                        apt.status = AppointmentStatus.COMPLETED
                        await self.update_appointment(apt)
                        logger.info(
                            "[AppointmentManager] Cita auto-completada: %s (horario ya pasó, era %s)",
                            phone, prev_status
                        )

                    # Solo incluir si:
                    # - No se ha enviado followup
                    # - Estado es completed (visita realizada o auto-completada)
                    # - CANCELLED y NO_SHOW no generan followup
                    if apt.status != AppointmentStatus.COMPLETED:
                        logger.info(
                            "[AppointmentManager] Skip followup %s — estado: %s",
                            phone, apt.status.value
                        )
                        continue

                    if not apt.followup_sent:
                        # Log detallado
                        apt_dt = apt.scheduled_dt
                        hours_since = (now - apt_dt).total_seconds() / 3600

                        logger.info(
                            "[AppointmentManager] Seguimiento incluido %s: "
                            "%.1f horas después de la cita, Estado %s",
                            phone, hours_since, apt.status.value
                        )

                        appointments.append(apt)

                except Exception as e:
                    logger.error(
                        "[AppointmentManager] Error procesando cita %s: %s",
                        key, e
                    )

        logger.info(
            "[AppointmentManager] Citas necesitando seguimiento: %d",
            len(appointments)
        )
        return appointments

    async def get_upcoming_appointments(
        self,
        phone_normalized: Optional[str] = None,
        limit: int = 10
    ) -> List[Appointment]:
        """
        Obtiene las próximas citas programadas.

        CORREGIDO: Usa datetime.now(TIMEZONE_BOGOTA).
        """
        r = await self._get_redis()
        now = get_bogota_now()

        # Obtener todas las citas futuras del índice
        keys = await r.zrangebyscore(
            self.APPOINTMENT_INDEX,
            now.timestamp(),
            "+inf",
            start=0,
            num=limit * 2  # Pedir más por si hay que filtrar
        )

        appointments = []
        for key in keys:
            # Filtrar por teléfono si se especificó
            if phone_normalized and phone_normalized not in key:
                continue

            data_str = await r.get(key)
            if data_str:
                try:
                    data = json.loads(data_str)
                    apt = Appointment.from_dict(data)

                    # Solo incluir citas activas
                    if apt.status in [AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]:
                        appointments.append(apt)

                    if len(appointments) >= limit:
                        break
                except Exception as e:
                    logger.error(
                        "[AppointmentManager] Error procesando cita %s: %s",
                        key, e
                    )

        return appointments


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def get_appointment_manager(redis_url: str = None) -> AppointmentManager:
    """Obtiene una instancia del AppointmentManager."""
    if redis_url is None:
        redis_url = os.getenv(
            "REDIS_PUBLIC_URL",
            os.getenv("REDIS_URL", "redis://localhost:6379")
        )
    return AppointmentManager(redis_url)