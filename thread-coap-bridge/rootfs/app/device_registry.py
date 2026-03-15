"""
Device Registry Module

SQLite database for managing discovered devices and their resources.
"""

import logging
import aiosqlite
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistrationResult:
    device_id: str
    is_new: bool
    resources_changed: bool
    needs_runtime_reconcile: bool
    commissioned: bool


class DeviceRegistry:
    """Device registry with SQLite backend."""

    def __init__(self, db_path='/data/devices.db'):
        self.db_path = db_path
        self.connection = None

        logger.info(f"Device Registry initialized (database: {db_path})")

    async def initialize(self):
        """Initialize SQLite database and create tables."""
        try:
            self.connection = await aiosqlite.connect(self.db_path)
            await self.connection.execute('PRAGMA foreign_keys = ON')
            await self._create_tables()
            await self._ensure_schema()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    async def _create_tables(self):
        """Create database schema."""
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    CREATE TABLE IF NOT EXISTS devices (
                        device_id TEXT PRIMARY KEY,
                        ipv6_address TEXT NOT NULL,
                        eui64 TEXT,
                        last_seen TIMESTAMP,
                        commissioned INTEGER DEFAULT 0,
                        consecutive_failures INTEGER DEFAULT 0,
                        is_online INTEGER DEFAULT 1,
                        auth_public_key TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                await cursor.execute('''
                    CREATE TABLE IF NOT EXISTS resources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT NOT NULL,
                        uri_path TEXT NOT NULL,
                        resource_type TEXT,
                        interface_type TEXT,
                        observable INTEGER DEFAULT 0,
                        FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE,
                        UNIQUE(device_id, uri_path)
                    )
                ''')

                await cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_devices_commissioned
                    ON devices(commissioned)
                ''')

                await cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_resources_device
                    ON resources(device_id)
                ''')

                await self.connection.commit()
                logger.debug("Database tables created successfully")

        except Exception as e:
            logger.error(f"Error creating database tables: {e}")
            raise

    async def _ensure_schema(self):
        """Apply lightweight schema migrations for existing databases."""
        async with self.connection.cursor() as cursor:
            await cursor.execute("PRAGMA table_info(devices)")
            columns = {row[1] for row in await cursor.fetchall()}

            if 'auth_public_key' not in columns:
                await cursor.execute("ALTER TABLE devices ADD COLUMN auth_public_key TEXT")

            await self.connection.commit()

    async def register_device(self, ipv6_address, eui64=None, resources=None):
        """Register or refresh a device and reconcile its resource set."""
        device_id = self._generate_device_id(ipv6_address, eui64)
        normalized_resources = self._normalize_resources(resources or [])

        logger.info(f"Registering device: {device_id} ({ipv6_address})")

        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT commissioned, is_online
                    FROM devices
                    WHERE device_id = ?
                ''', (device_id,))
                existing = await cursor.fetchone()

                is_new = existing is None
                previous_commissioned = bool(existing[0]) if existing else False
                previous_online = bool(existing[1]) if existing else False
                existing_resources = await self._get_resource_map(cursor, device_id)
                resources_changed = normalized_resources != existing_resources

                needs_runtime_reconcile = is_new or resources_changed or not previous_online
                next_commissioned = 0 if needs_runtime_reconcile else int(previous_commissioned)

                await cursor.execute('''
                    INSERT INTO devices (
                        device_id, ipv6_address, eui64, last_seen,
                        commissioned, consecutive_failures, is_online
                    )
                    VALUES (?, ?, ?, ?, ?, 0, 1)
                    ON CONFLICT(device_id) DO UPDATE SET
                        ipv6_address = excluded.ipv6_address,
                        last_seen = excluded.last_seen,
                        commissioned = excluded.commissioned,
                        consecutive_failures = 0,
                        is_online = 1
                ''', (device_id, ipv6_address, eui64, datetime.now(), next_commissioned))

                for resource in normalized_resources.values():
                    await cursor.execute('''
                        INSERT OR REPLACE INTO resources
                        (device_id, uri_path, resource_type, interface_type, observable)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        device_id,
                        resource['uri_path'],
                        resource['resource_type'],
                        resource['interface_type'],
                        resource['observable'],
                    ))

                removed_paths = sorted(set(existing_resources.keys()) - set(normalized_resources.keys()))
                if removed_paths:
                    placeholders = ','.join('?' for _ in removed_paths)
                    await cursor.execute(
                        f"DELETE FROM resources WHERE device_id = ? AND uri_path IN ({placeholders})",
                        [device_id, *removed_paths],
                    )

                await self.connection.commit()
                logger.info(
                    "Device %s registered with %d resources (changed=%s, runtime_reconcile=%s)",
                    device_id,
                    len(normalized_resources),
                    resources_changed,
                    needs_runtime_reconcile,
                )

        except Exception as e:
            logger.error(f"Error registering device {device_id}: {e}")
            raise

        return RegistrationResult(
            device_id=device_id,
            is_new=is_new,
            resources_changed=resources_changed,
            needs_runtime_reconcile=needs_runtime_reconcile,
            commissioned=bool(next_commissioned),
        )

    async def _get_resource_map(self, cursor, device_id) -> Dict[str, Dict[str, object]]:
        await cursor.execute('''
            SELECT uri_path, resource_type, interface_type, observable
            FROM resources
            WHERE device_id = ?
        ''', (device_id,))
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            result[row[0]] = {
                'uri_path': row[0],
                'resource_type': (row[1] or 'unknown').lower(),
                'interface_type': row[2],
                'observable': int(bool(row[3])),
            }
        return result

    def _normalize_resources(self, resources) -> Dict[str, Dict[str, object]]:
        normalized: Dict[str, Dict[str, object]] = {}
        for resource in resources:
            uri_path = resource['uri_path']
            normalized[uri_path] = {
                'uri_path': uri_path,
                'resource_type': (resource.get('resource_type') or 'unknown').lower(),
                'interface_type': resource.get('interface_type'),
                'observable': 1 if resource.get('observable', False) else 0,
            }
        return normalized

    async def get_uncommissioned_devices(self):
        """Get devices that need runtime reconciliation."""
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT device_id, ipv6_address, eui64, last_seen, commissioned, is_online, auth_public_key
                    FROM devices
                    WHERE commissioned = 0
                ''')

                rows = await cursor.fetchall()
                devices = []
                for row in rows:
                    devices.append(
                        Device(
                            device_id=row[0],
                            ipv6_address=row[1],
                            eui64=row[2],
                            last_seen=datetime.fromisoformat(row[3]) if row[3] else None,
                            commissioned=bool(row[4]),
                            is_online=bool(row[5]),
                            auth_public_key=row[6],
                        )
                    )
                return devices

        except Exception as e:
            logger.error(f"Error getting uncommissioned devices: {e}")
            return []

    async def get_all_devices(self):
        """Get all registered devices."""
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT device_id, ipv6_address, eui64, last_seen, commissioned, is_online, auth_public_key
                    FROM devices
                ''')

                rows = await cursor.fetchall()
                devices = []
                for row in rows:
                    devices.append(
                        Device(
                            device_id=row[0],
                            ipv6_address=row[1],
                            eui64=row[2],
                            last_seen=datetime.fromisoformat(row[3]) if row[3] else None,
                            commissioned=bool(row[4]),
                            is_online=bool(row[5]),
                            auth_public_key=row[6],
                        )
                    )
                return devices

        except Exception as e:
            logger.error(f"Error getting all devices: {e}")
            return []

    async def get_device_by_id(self, device_id):
        """Get device by ID."""
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT device_id, ipv6_address, eui64, last_seen, commissioned, is_online, auth_public_key
                    FROM devices
                    WHERE device_id = ?
                ''', (device_id,))

                row = await cursor.fetchone()

                if row:
                    return Device(
                        device_id=row[0],
                        ipv6_address=row[1],
                        eui64=row[2],
                        last_seen=datetime.fromisoformat(row[3]) if row[3] else None,
                        commissioned=bool(row[4]),
                        is_online=bool(row[5]),
                        auth_public_key=row[6],
                    )
                return None

        except Exception as e:
            logger.error(f"Error getting device {device_id}: {e}")
            return None

    async def get_device_resources(self, device_id):
        """Get all resources for a device."""
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT uri_path, resource_type, interface_type, observable
                    FROM resources
                    WHERE device_id = ?
                    ORDER BY uri_path
                ''', (device_id,))

                rows = await cursor.fetchall()
                return [
                    Resource(
                        uri_path=row[0],
                        resource_type=row[1],
                        interface_type=row[2],
                        observable=bool(row[3]),
                    )
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"Error getting resources for device {device_id}: {e}")
            return []

    async def mark_commissioned(self, device_id):
        """Mark device as reconciled in the runtime."""
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    UPDATE devices
                    SET commissioned = 1
                    WHERE device_id = ?
                ''', (device_id,))
                await self.connection.commit()
        except Exception as e:
            logger.error(f"Error marking device commissioned: {e}")

    async def set_device_auth_public_key(self, device_id, public_key_hex):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    UPDATE devices
                    SET auth_public_key = ?
                    WHERE device_id = ?
                ''', (public_key_hex, device_id))
                await self.connection.commit()
        except Exception as e:
            logger.error(f"Error storing auth public key for {device_id}: {e}")

    async def get_device_auth_public_key(self, device_id):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT auth_public_key
                    FROM devices
                    WHERE device_id = ?
                ''', (device_id,))
                row = await cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Error loading auth public key for {device_id}: {e}")
            return None

    async def update_last_seen(self, device_id):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    UPDATE devices
                    SET last_seen = ?
                    WHERE device_id = ?
                ''', (datetime.now(), device_id))
                await self.connection.commit()
        except Exception as e:
            logger.error(f"Error updating last_seen for {device_id}: {e}")

    async def decommission_device(self, device_id):
        logger.info(f"Decommissioning device: {device_id}")
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    DELETE FROM devices
                    WHERE device_id = ?
                ''', (device_id,))
                await self.connection.commit()
        except Exception as e:
            logger.error(f"Error decommissioning device {device_id}: {e}")

    async def update_device_failure(self, device_id, failed=True):
        try:
            async with self.connection.cursor() as cursor:
                if failed:
                    await cursor.execute('''
                        UPDATE devices
                        SET consecutive_failures = consecutive_failures + 1
                        WHERE device_id = ?
                    ''', (device_id,))
                else:
                    await cursor.execute('''
                        UPDATE devices
                        SET consecutive_failures = 0,
                            last_seen = ?,
                            is_online = 1
                        WHERE device_id = ?
                    ''', (datetime.now(), device_id))
                await self.connection.commit()
        except Exception as e:
            logger.error(f"Error updating device failure state for {device_id}: {e}")

    async def mark_device_offline(self, device_id):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    UPDATE devices
                    SET is_online = 0
                    WHERE device_id = ?
                ''', (device_id,))
                await self.connection.commit()
                logger.info(f"Device {device_id} marked as offline in database")
        except Exception as e:
            logger.error(f"Error marking device offline: {e}")

    async def mark_all_devices_offline(self):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    UPDATE devices
                    SET is_online = 0
                    WHERE is_online != 0
                ''')
                updated = cursor.rowcount or 0
                await self.connection.commit()
                if updated > 0:
                    logger.info("Marked %d stored device(s) offline in database", updated)
        except Exception as e:
            logger.error(f"Error marking all devices offline: {e}")

    async def get_offline_devices(self):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT device_id, ipv6_address, eui64, last_seen, commissioned, is_online, auth_public_key
                    FROM devices
                    WHERE is_online = 0 AND commissioned = 1
                ''')

                rows = await cursor.fetchall()
                return [
                    Device(
                        device_id=row[0],
                        ipv6_address=row[1],
                        eui64=row[2],
                        last_seen=datetime.fromisoformat(row[3]) if row[3] else None,
                        commissioned=bool(row[4]),
                        is_online=bool(row[5]),
                        auth_public_key=row[6],
                    )
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"Error getting offline devices: {e}")
            return []

    async def get_devices_for_cleanup(self, offline_hours=24):
        try:
            async with self.connection.cursor() as cursor:
                cutoff_time = datetime.now() - timedelta(hours=offline_hours)
                await cursor.execute('''
                    SELECT device_id, ipv6_address, eui64, last_seen, commissioned, is_online, auth_public_key
                    FROM devices
                    WHERE is_online = 0
                      AND last_seen < ?
                      AND commissioned = 1
                ''', (cutoff_time,))

                rows = await cursor.fetchall()
                return [
                    Device(
                        device_id=row[0],
                        ipv6_address=row[1],
                        eui64=row[2],
                        last_seen=datetime.fromisoformat(row[3]) if row[3] else None,
                        commissioned=bool(row[4]),
                        is_online=bool(row[5]),
                        auth_public_key=row[6],
                    )
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"Error getting devices for cleanup: {e}")
            return []

    async def get_device_failure_count(self, device_id):
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute('''
                    SELECT consecutive_failures
                    FROM devices
                    WHERE device_id = ?
                ''', (device_id,))
                row = await cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"Error getting failure count for {device_id}: {e}")
            return 0

    async def close(self):
        if self.connection:
            await self.connection.close()
            logger.info("Database connection closed")

    def _generate_device_id(self, ipv6_address, eui64=None):
        if eui64:
            return f"thread_{eui64.replace(':', '')}"

        parts = ipv6_address.split(':')
        suffix = ''.join(parts[-4:]) if len(parts) >= 4 else ipv6_address.replace(':', '')
        return f"thread_{suffix}"


class Device:
    """Device model."""

    def __init__(self, device_id, ipv6_address, eui64=None,
                 last_seen=None, commissioned=False, is_online=True,
                 auth_public_key=None):
        self.device_id = device_id
        self.ipv6_address = ipv6_address
        self.eui64 = eui64
        self.last_seen = last_seen or datetime.now()
        self.commissioned = commissioned
        self.is_online = is_online
        self.auth_public_key = auth_public_key


class Resource:
    """Resource model."""

    def __init__(self, uri_path, resource_type, interface_type=None,
                 observable=False):
        self.uri_path = uri_path
        self.resource_type = resource_type
        self.interface_type = interface_type
        self.observable = observable
