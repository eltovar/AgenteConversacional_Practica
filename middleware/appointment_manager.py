# middleware/appointment_manager.py
"""
Gestor de Citas en Redis para el sistema de recordatorios automáticos.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from enum import Enum
from zoneinfo import ZoneInfo

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Timezone de Colombia
TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")


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
    scheduled_datetime: str       # ISO format
    status: AppointmentStatus = AppointmentStatus.PENDING
    reminder_sent: bool = False
    followup_sent: bool = False
    created_at: Optional[str] = None
    contact_name: Optional[str] = None
    contact_id: Optional[str] = None
    notes: Optional[str] = None

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
        return cls(**data)

    @property
    def scheduled_dt(self) -> datetime:
        """Retorna el datetime de la cita."""
        dt = datetime.fromisoformat(self.scheduled_datetime)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE_BOGOTA)
        return dt


class AppointmentManager:
    """Gestor de citas en Redis."""

    APPOINTMENT_PREFIX = "appointment:"
    APPOINTMENT_INDEX = "appointment_index"  # Sorted set para búsquedas por tiempo

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

        Args:
            phone_normalized: Teléfono en formato E.164
            canal: Canal de origen
            scheduled_datetime: Fecha y hora de la cita
            contact_name: Nombre del cliente
            contact_id: ID en HubSpot
            notes: Notas adicionales

        Returns:
            Appointment creada
        """
        r = await self._get_redis()

        # Asegurar timezone
        if scheduled_datetime.tzinfo is None:
            scheduled_datetime = scheduled_datetime.replace(tzinfo=TIMEZONE_BOGOTA)

        appointment = Appointment(
            phone_normalized=phone_normalized,
            canal=canal,
            scheduled_datetime=scheduled_datetime.isoformat(),
            created_at=datetime.now(timezone.utc).isoformat(),
            contact_name=contact_name,
            contact_id=contact_id,
            notes=notes
        )

        key = self._build_key(phone_normalized, canal)

        # Guardar cita como JSON
        await r.set(key, json.dumps(appointment.to_dict()), ex=self.APPOINTMENT_TTL)

        # Agregar al índice ordenado por fecha
        # Score = timestamp de la cita para búsquedas eficientes
        await r.zadd(
            self.APPOINTMENT_INDEX,
            {key: scheduled_datetime.timestamp()}
        )

        logger.info(
            f"[AppointmentManager] Cita creada: {phone_normalized}:{canal} "
            f"para {scheduled_datetime.strftime('%Y-%m-%d %H:%M')}"
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
            logger.error(f"[AppointmentManager] Error deserializando cita: {e}")
            return None

    async def update_appointment(self, appointment: Appointment) -> bool:
        """Actualiza una cita existente."""
        r = await self._get_redis()
        key = self._build_key(appointment.phone_normalized, appointment.canal)

        try:
            await r.set(key, json.dumps(appointment.to_dict()), ex=self.APPOINTMENT_TTL)
            return True
        except Exception as e:
            logger.error(f"[AppointmentManager] Error actualizando cita: {e}")
            return False

    async def mark_reminder_sent(self, phone_normalized: str, canal: str) -> bool:
        """Marca que se envió el recordatorio (24h antes)."""
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        appointment.reminder_sent = True
        return await self.update_appointment(appointment)

    async def mark_followup_sent(self, phone_normalized: str, canal: str) -> bool:
        """Marca que se envió el seguimiento post-cita (24h después)."""
        appointment = await self.get_appointment(phone_normalized, canal)
        if not appointment:
            return False

        appointment.followup_sent = True
        return await self.update_appointment(appointment)

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
            logger.info(f"[AppointmentManager] Cita completada: {phone_normalized}:{canal}")

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
            logger.info(f"[AppointmentManager] Cita cancelada: {phone_normalized}:{canal}")

        return success

    async def get_appointments_needing_reminder(self) -> List[Appointment]:
        """
        Obtiene citas que necesitan recordatorio (24h antes).

        Busca citas programadas entre 23h y 25h desde ahora
        (ventana de 2h para el job que corre cada hora).

        Returns:
            Lista de Appointments que necesitan recordatorio
        """
        r = await self._get_redis()
        now = datetime.now(timezone.utc)

        # Buscar citas programadas entre 23h y 25h desde ahora
        min_time = now + timedelta(hours=23)
        max_time = now + timedelta(hours=25)

        # Obtener keys del índice
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

                    # Solo incluir si:
                    # - No se ha enviado recordatorio
                    # - Estado es pending o confirmed
                    if (not apt.reminder_sent and
                        apt.status in [AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]):
                        appointments.append(apt)
                except Exception as e:
                    logger.error(f"[AppointmentManager] Error procesando cita {key}: {e}")

        logger.info(f"[AppointmentManager] Citas necesitando recordatorio: {len(appointments)}")
        return appointments

    async def get_appointments_needing_followup(self) -> List[Appointment]:
        """
        Obtiene citas que necesitan seguimiento (24h después).

        Busca citas que fueron completadas hace 23-25h.

        Returns:
            Lista de Appointments que necesitan followup
        """
        r = await self._get_redis()
        now = datetime.now(timezone.utc)

        # Buscar citas que fueron hace 23-25h
        min_time = now - timedelta(hours=25)
        max_time = now - timedelta(hours=23)

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

                    # Solo incluir si:
                    # - No se ha enviado followup
                    # - Estado es completed (visita realizada)
                    if (not apt.followup_sent and
                        apt.status == AppointmentStatus.COMPLETED):
                        appointments.append(apt)
                except Exception as e:
                    logger.error(f"[AppointmentManager] Error procesando cita {key}: {e}")

        logger.info(f"[AppointmentManager] Citas necesitando seguimiento: {len(appointments)}")
        return appointments

    async def get_upcoming_appointments(
        self,
        phone_normalized: Optional[str] = None,
        limit: int = 10
    ) -> List[Appointment]:
        """
        Obtiene las próximas citas programadas.

        Args:
            phone_normalized: Si se especifica, solo citas de este teléfono
            limit: Número máximo de citas a retornar

        Returns:
            Lista de próximas citas ordenadas por fecha
        """
        r = await self._get_redis()
        now = datetime.now(timezone.utc)

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
                    logger.error(f"[AppointmentManager] Error procesando cita {key}: {e}")

        return appointments


# Función helper para obtener instancia
def get_appointment_manager(redis_url: str = None) -> AppointmentManager:
    """Obtiene una instancia del AppointmentManager."""
    import os
    if redis_url is None:
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
    return AppointmentManager(redis_url)