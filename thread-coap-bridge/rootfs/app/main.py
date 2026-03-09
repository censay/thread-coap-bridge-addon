#!/usr/bin/env python3
"""
Thread CoAP to MQTT Bridge - Main Entry Point

This service bridges CoAP devices on Thread networks to Home Assistant via MQTT.
"""

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, Prehashed
from config_handler import ConfigHandler
from device_registry import DeviceRegistry
from mqtt_publisher import MQTTPublisher
from coap_discovery import CoAPDiscovery
from coap_client import CoAPClient
from resource_handlers import ResourceHandlerRegistry, ResourceRecord

logger = logging.getLogger(__name__)


class CoAPBridgeService:
    """Main bridge service orchestrator."""

    def __init__(self):
        self.config = ConfigHandler()
        self.running = True

        self.registry = None
        self.mqtt = None
        self.discovery = None
        self.coap_client = None

        self.background_tasks = []
        self.resource_tasks = {}
        self.active_entities = {}
        self.reconcile_requested = set()

        self.recent_commands = {}
        self.command_suppress_time = 10

        self.device_uptimes = {}
        self.sensor_failures = {}
        self.sensor_available = {}
        self.sensor_offline_threshold = 3

        self.handlers = ResourceHandlerRegistry()
        self.auth_state = {}
        self.auth_expiry_tasks = {}
        self.auth_ttl_seconds = 30 * 60

        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        logger.info("CoAP Bridge Service initialized")

    def _handle_shutdown(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False

    def _track_background_task(self, task):
        self.background_tasks.append(task)
        return task

    def _track_resource_task(self, device_id, source_uri, spec, task):
        self.resource_tasks[(device_id, source_uri)] = {
            'task': task,
            'spec': spec,
        }
        return task

    def _prune_finished_tasks(self):
        for task in self.background_tasks[:]:
            if task.done():
                if not task.cancelled():
                    exc = task.exception()
                    if exc:
                        logger.error(f"Task {task.get_name()} crashed with exception: {exc}")
                self.background_tasks.remove(task)

        for key, entry in list(self.resource_tasks.items()):
            task = entry['task']
            if task.done():
                if not task.cancelled():
                    exc = task.exception()
                    if exc:
                        logger.error(f"Resource task {task.get_name()} crashed with exception: {exc}")
                    self.reconcile_requested.add(key[0])
                elif task.get_name().startswith(("observe_", "poll_")):
                    self.reconcile_requested.add(key[0])
                del self.resource_tasks[key]

        for device_id, task in list(self.auth_expiry_tasks.items()):
            if task.done():
                if not task.cancelled():
                    exc = task.exception()
                    if exc:
                        logger.error(f"Auth timer for {device_id} crashed with exception: {exc}")
                del self.auth_expiry_tasks[device_id]

    async def start(self):
        logger.info("=" * 60)
        logger.info("Starting Thread CoAP-MQTT Bridge")
        logger.info("=" * 60)

        logger.info("Configuration:")
        logger.info(f"  MQTT Host: {self.config.get('mqtt_host')}")
        logger.info(f"  MQTT Port: {self.config.get('mqtt_port')}")
        logger.info(f"  Discovery Interval: {self.config.get('discovery_interval')}s")
        logger.info(f"  Thread Interface: {self.config.get('thread_interface')}")
        logger.info(f"  Multicast Address: {self.config.get('multicast_address')}")

        try:
            logger.info("Initializing device registry...")
            self.registry = DeviceRegistry(db_path='/data/devices.db')
            await self.registry.initialize()

            logger.info("Connecting to MQTT broker...")
            self.mqtt = MQTTPublisher(self.config.mqtt_config)
            await self.mqtt.connect()
            self.mqtt.set_command_callback(self._handle_mqtt_command)

            logger.info("Initializing CoAP client...")
            self.coap_client = CoAPClient(self.mqtt)
            self.coap_client.set_status_callback(self._handle_device_status_change)
            await self.coap_client.initialize()

            logger.info("Initializing CoAP discovery...")
            self.discovery = CoAPDiscovery(self.registry, self.config.coap_config)
            await self.discovery.initialize()

            await self._republish_all_discovery()

            logger.info("Starting background tasks...")
            self._track_background_task(asyncio.create_task(self._discovery_loop(), name="discovery_loop"))
            self._track_background_task(asyncio.create_task(self._monitor_devices(), name="device_monitor"))
            self._track_background_task(asyncio.create_task(self._cleanup_loop(), name="cleanup_loop"))

            logger.info("=" * 60)
            logger.info("Service started successfully")
            logger.info("Bridge is now running - monitoring for CoAP devices...")
            logger.info("=" * 60)

            while self.running:
                self._prune_finished_tasks()
                await asyncio.sleep(5)

            logger.info("Shutdown initiated...")
            await self._cleanup()

        except Exception as e:
            logger.exception(f"Fatal error in main loop: {e}")
            sys.exit(1)

    async def _republish_all_discovery(self):
        logger.info("Re-publishing MQTT discovery for all known devices...")

        try:
            all_devices = await self.registry.get_all_devices()
            logger.info(f"Found {len(all_devices)} devices in registry")

            for device in all_devices:
                await self._reconcile_device(device, mark_commissioned=False)

            logger.info(f"Re-published discovery for {len(all_devices)} devices")

        except Exception as e:
            logger.error(f"Error re-publishing discovery: {e}")

    async def _discovery_loop(self):
        interval = self.config.get('discovery_interval', 60)
        logger.info(f"Starting discovery loop (interval: {interval}s)")

        while self.running:
            try:
                results = []
                results.extend(await self.discovery.discover_devices())
                results.extend(await self.discovery.rediscover_offline_devices(self.registry))
                if results:
                    logger.info(f"Discovery cycle reconciled {len(results)} device response(s)")
                    for result in results:
                        if not result.commissioned:
                            self.reconcile_requested.add(result.device_id)
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"Error in discovery loop: {e}")
                await asyncio.sleep(interval)

    async def _monitor_devices(self):
        logger.info("Starting device monitor")

        while self.running:
            try:
                uncommissioned = await self.registry.get_uncommissioned_devices()
                queued_reconciles = set(self.reconcile_requested)
                self.reconcile_requested.clear()
                devices_to_reconcile = {device.device_id: device for device in uncommissioned}

                for device_id in queued_reconciles:
                    if device_id not in devices_to_reconcile:
                        device = await self.registry.get_device_by_id(device_id)
                        if device:
                            devices_to_reconcile[device.device_id] = device

                for device in devices_to_reconcile.values():
                    logger.info(f"Reconciling runtime for {device.device_id}")
                    await self._reconcile_device(device, mark_commissioned=True)

                await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"Error in device monitor: {e}")
                await asyncio.sleep(10)

    async def _resolve_entities(self, device, resources):
        entities = {}
        for resource in resources:
            record = ResourceRecord(
                uri_path=resource.uri_path,
                resource_type=resource.resource_type,
                interface_type=resource.interface_type,
                observable=resource.observable,
            )
            resolved = await self.handlers.build_entities(self.coap_client, device, record)
            for entity in resolved:
                entities[entity.object_id] = entity
        return entities

    async def _reconcile_device(self, device, mark_commissioned=True):
        resources = await self.registry.get_device_resources(device.device_id)
        desired_entities = await self._resolve_entities(device, resources)
        current_entities = self.active_entities.get(device.device_id, {})

        for object_id, entity in current_entities.items():
            if object_id not in desired_entities:
                self.mqtt.publish_discovery_removal(entity.component, device.device_id, object_id)

        for entity in desired_entities.values():
            self.mqtt.publish_entity_discovery(device.device_id, entity, device.ipv6_address)

        self.active_entities[device.device_id] = desired_entities
        await self._sync_monitor_tasks(device, desired_entities)

        if any((resource.resource_type or '').lower() == 'auth' for resource in resources):
            await self._bootstrap_auth_device(device)
        else:
            await self._clear_auth_state(device.device_id, publish_state=False)

        self.mqtt.publish_availability(device.device_id, available=getattr(device, 'is_online', True))

        if mark_commissioned:
            await self.registry.mark_commissioned(device.device_id)

    async def _sync_monitor_tasks(self, device, desired_entities):
        desired_monitors = self.handlers.build_monitor_specs(desired_entities)
        current_keys = {
            source_uri for (device_id, source_uri) in self.resource_tasks.keys()
            if device_id == device.device_id
        }
        desired_keys = set(desired_monitors.keys())

        for source_uri in current_keys - desired_keys:
            await self._cancel_resource_task(device.device_id, source_uri)

        for source_uri, spec in desired_monitors.items():
            current = self.resource_tasks.get((device.device_id, source_uri))
            if current and current['spec'] == spec and not current['task'].done():
                continue

            if current:
                await self._cancel_resource_task(device.device_id, source_uri)

            task = self._start_resource_task(device, spec)
            self._track_resource_task(device.device_id, source_uri, spec, task)

    def _start_resource_task(self, device, spec):
        if spec.monitor_mode == 'observe':
            return asyncio.create_task(
                self.coap_client.observe_resource(
                    device.device_id,
                    device.ipv6_address,
                    spec.source_uri,
                    registry=self.registry,
                    offline_threshold=self.config.get('offline_threshold_polls', 5),
                    discovery=self.discovery,
                ),
                name=f"observe_{device.device_id}_{spec.source_uri}",
            )

        return asyncio.create_task(
            self._poll_resource(
                device.device_id,
                device.ipv6_address,
                spec.source_uri,
                spec.state_uri,
                spec.resource_type,
                interval=spec.poll_interval,
                initial_delay=spec.initial_delay,
                sensor_availability=spec.sensor_availability,
            ),
            name=f"poll_{device.device_id}_{spec.source_uri}",
        )

    async def _cancel_resource_task(self, device_id, source_uri):
        key = (device_id, source_uri)
        entry = self.resource_tasks.pop(key, None)
        if not entry:
            return

        task = entry['task']
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _poll_resource(self, device_id, ipv6_addr, source_uri, state_uri, resource_type,
                             interval=120, initial_delay=0, sensor_availability=False):
        sensor_key = (device_id, state_uri)
        object_id = state_uri.strip('/')
        offline_threshold = self.config.get('offline_threshold_polls', 5)
        consecutive_failures = 0
        device_is_online = True

        logger.info(
            "Starting polling for %s%s -> %s (interval=%ss, initial_delay=%ss)",
            device_id,
            source_uri,
            state_uri,
            interval,
            initial_delay,
        )

        self.sensor_failures[sensor_key] = 0
        self.sensor_available[sensor_key] = True

        if sensor_availability:
            self.mqtt.publish_sensor_availability(device_id, object_id, True)

        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        while self.running:
            try:
                payload = await self.coap_client.get_resource(ipv6_addr, source_uri)
                if not payload:
                    logger.info(f"Retrying {source_uri} after 10s...")
                    await asyncio.sleep(10)
                    payload = await self.coap_client.get_resource(ipv6_addr, source_uri)

                if payload:
                    normalized = await self._normalize_polled_state(device_id, resource_type, payload)
                    if normalized is None:
                        self._handle_sensor_failure(device_id, state_uri, object_id, sensor_availability)
                    else:
                        if consecutive_failures > 0:
                            logger.info(
                                "Resource %s recovered after %d failures",
                                source_uri,
                                consecutive_failures,
                            )
                        consecutive_failures = 0
                        self.sensor_failures[sensor_key] = 0

                        await self.registry.update_device_failure(device_id, failed=False)

                        if not device_is_online:
                            self.mqtt.publish_availability(device_id, available=True)
                            await self._handle_device_status_change(device_id, True)
                            device_is_online = True

                        if sensor_availability and not self.sensor_available.get(sensor_key, True):
                            self.sensor_available[sensor_key] = True
                            self.mqtt.publish_sensor_availability(device_id, object_id, True)

                        if self._should_publish_state(device_id, object_id, normalized):
                            self.mqtt.publish_state(device_id, state_uri, normalized)
                else:
                    consecutive_failures += 1
                    await self.registry.update_device_failure(device_id, failed=True)
                    logger.warning(
                        "Poll failed for %s%s (%d/%d failures)",
                        device_id,
                        source_uri,
                        consecutive_failures,
                        offline_threshold,
                    )

                    if consecutive_failures >= offline_threshold and device_is_online:
                        self.mqtt.publish_availability(device_id, available=False)
                        await self.registry.mark_device_offline(device_id)
                        await self._handle_device_status_change(device_id, False)
                        device_is_online = False

                    self._handle_sensor_failure(device_id, state_uri, object_id, sensor_availability)

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info(f"Polling cancelled for {device_id}{source_uri}")
                break
            except Exception as e:
                logger.error(f"Error polling resource {source_uri}: {e}")
                consecutive_failures += 1
                await self.registry.update_device_failure(device_id, failed=True)
                self._handle_sensor_failure(device_id, state_uri, object_id, sensor_availability)
                await asyncio.sleep(interval)

    async def _normalize_polled_state(self, device_id, resource_type, payload):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {'value': payload}

        if resource_type == 'uptime':
            value = data.get('value')
            if value is None:
                return {'value': json.dumps(data, sort_keys=True)}

            last_uptime = self.device_uptimes.get(device_id)
            if last_uptime is not None and value < last_uptime:
                logger.warning(
                    "Device %s rebooted! Uptime went from %sms to %sms",
                    device_id,
                    last_uptime,
                    value,
                )
                await self.coap_client.reregister_observers(device_id)

            self.device_uptimes[device_id] = value
            return {'value': value // 1000}

        if resource_type == 'voltage':
            value = data.get('value')
            if value is None:
                return {'value': json.dumps(data, sort_keys=True)}
            return {'value': f"{value / 1000:.2f}"}

        if 'value' in data:
            return {'value': data['value']}

        if 'state' in data:
            return {'value': data['state']}

        return {'value': json.dumps(data, sort_keys=True)}

    def _handle_sensor_failure(self, device_id, state_uri, object_id, sensor_availability):
        if not sensor_availability:
            return

        sensor_key = (device_id, state_uri)
        self.sensor_failures[sensor_key] = self.sensor_failures.get(sensor_key, 0) + 1
        failure_count = self.sensor_failures[sensor_key]
        logger.warning(f"Sensor {state_uri} failure #{failure_count}")

        if failure_count >= self.sensor_offline_threshold:
            if self.sensor_available.get(sensor_key, True):
                self.sensor_available[sensor_key] = False
                self.mqtt.publish_sensor_availability(device_id, object_id, False)
                logger.warning(
                    "Sensor %s/%s marked offline after %d consecutive failures",
                    device_id,
                    object_id,
                    failure_count,
                )

    async def _cleanup_loop(self):
        cleanup_interval = self.config.get('cleanup_check_interval', 3600)
        cleanup_threshold_hours = self.config.get('cleanup_after_hours', 24)

        logger.info(
            f"Starting cleanup loop (check interval: {cleanup_interval}s, cleanup after: {cleanup_threshold_hours}h)"
        )

        while self.running:
            try:
                await asyncio.sleep(cleanup_interval)
                devices_to_cleanup = await self.registry.get_devices_for_cleanup(
                    offline_hours=cleanup_threshold_hours,
                )

                for device in devices_to_cleanup:
                    logger.info(f"Cleaning up device {device.device_id}")
                    await self._remove_device_runtime(device.device_id)
                    await self.registry.decommission_device(device.device_id)

            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
                await asyncio.sleep(cleanup_interval)

    async def _remove_device_runtime(self, device_id):
        for (tracked_device_id, source_uri) in list(self.resource_tasks.keys()):
            if tracked_device_id == device_id:
                await self._cancel_resource_task(device_id, source_uri)

        entities = self.active_entities.pop(device_id, {})
        for entity in entities.values():
            self.mqtt.publish_discovery_removal(entity.component, device_id, entity.object_id)

        await self._clear_auth_state(device_id, publish_state=False)
        self.recent_commands = {
            key: value for key, value in self.recent_commands.items()
            if key[0] != device_id
        }

    async def _handle_device_status_change(self, device_id, is_online):
        if is_online:
            if device_id not in self.auth_state:
                self.auth_state[device_id] = {'tier': 1}
            return

        await self._clear_auth_state(device_id, publish_state=True)

    async def _bootstrap_auth_device(self, device):
        payload = await self.coap_client.get_resource(device.ipv6_address, '/auth')
        tier_hint = 1
        public_key_hex = None

        if payload:
            try:
                data = json.loads(payload)
                tier_hint = data.get('tier', 1)
                public_key_hex = data.get('pubkey')
            except json.JSONDecodeError as exc:
                logger.warning(f"Failed to parse /auth GET for {device.device_id}: {exc}")

        if public_key_hex:
            await self.registry.set_device_auth_public_key(device.device_id, public_key_hex)
        else:
            public_key_hex = await self.registry.get_device_auth_public_key(device.device_id)

        current_tier = self.auth_state.get(device.device_id, {}).get('tier', tier_hint)
        await self._set_auth_tier(device.device_id, max(current_tier, tier_hint))

        if not public_key_hex:
            logger.warning(f"No auth public key available yet for {device.device_id}")

    async def _clear_auth_state(self, device_id, publish_state=True):
        timer = self.auth_expiry_tasks.pop(device_id, None)
        if timer and not timer.done():
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass

        self.auth_state[device_id] = {'tier': 1}
        if publish_state and self._has_entity(device_id, 'auth_tier'):
            self.mqtt.publish_state(device_id, '/auth_tier', {'value': 1})

    async def _set_auth_tier(self, device_id, tier):
        self.auth_state[device_id] = {'tier': tier}
        if self._has_entity(device_id, 'auth_tier'):
            self.mqtt.publish_state(device_id, '/auth_tier', {'value': tier})

    def _schedule_auth_expiry(self, device_id):
        existing = self.auth_expiry_tasks.get(device_id)
        if existing and not existing.done():
            existing.cancel()

        task = asyncio.create_task(
            self._auth_expiry_worker(device_id),
            name=f"auth_expiry_{device_id}",
        )
        self.auth_expiry_tasks[device_id] = task

    async def _auth_expiry_worker(self, device_id):
        try:
            await asyncio.sleep(self.auth_ttl_seconds)
            logger.info(f"Auth tier expired for {device_id}")
            await self._set_auth_tier(device_id, 1)
        except asyncio.CancelledError:
            raise

    def _has_entity(self, device_id, object_id):
        return object_id in self.active_entities.get(device_id, {})

    async def _handle_auth_request(self, device):
        if not self._has_entity(device.device_id, 'auth_request'):
            logger.warning(f"Auth request ignored for {device.device_id}: auth entity not active")
            return

        payload = await self.coap_client.get_resource(device.ipv6_address, '/auth')
        public_key_hex = None

        if payload:
            try:
                data = json.loads(payload)
                public_key_hex = data.get('pubkey')
            except json.JSONDecodeError as exc:
                logger.warning(f"Failed to parse /auth bootstrap for {device.device_id}: {exc}")

        if public_key_hex:
            await self.registry.set_device_auth_public_key(device.device_id, public_key_hex)
        else:
            public_key_hex = await self.registry.get_device_auth_public_key(device.device_id)

        if not public_key_hex:
            logger.warning(f"No auth public key available for {device.device_id}")
            return

        nonce = os.urandom(32)
        response = await self.coap_client.post_resource(device.ipv6_address, '/auth', nonce)
        if not response or response is True:
            logger.warning(f"Auth POST failed for {device.device_id}")
            return

        try:
            data = json.loads(response)
        except json.JSONDecodeError as exc:
            logger.warning(f"Invalid auth POST response from {device.device_id}: {exc}")
            return

        signature_hex = data.get('signature')
        if not signature_hex:
            logger.warning(f"Auth POST response missing signature for {device.device_id}")
            return

        if self._verify_auth_signature(public_key_hex, nonce, signature_hex):
            logger.info(f"Auth verification successful for {device.device_id}")
            await self._set_auth_tier(device.device_id, 2)
            self._schedule_auth_expiry(device.device_id)
        else:
            logger.warning(f"Auth verification failed for {device.device_id}")

    def _verify_auth_signature(self, public_key_hex, nonce, signature_hex):
        try:
            public_key_bytes = bytes.fromhex(public_key_hex)
            signature_bytes = bytes.fromhex(signature_hex)
            if len(signature_bytes) != 64:
                logger.warning(f"Unexpected auth signature length: {len(signature_bytes)}")
                return False

            public_key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(),
                public_key_bytes,
            )

            r = int.from_bytes(signature_bytes[:32], 'big')
            s = int.from_bytes(signature_bytes[32:], 'big')
            der_signature = encode_dss_signature(r, s)
            nonce_hash = hashlib.sha256(nonce).digest()

            public_key.verify(
                der_signature,
                nonce_hash,
                ec.ECDSA(Prehashed(hashes.SHA256())),
            )
            return True
        except Exception as exc:
            logger.warning(f"Auth signature verification error: {exc}")
            return False

    def _extract_led_state(self, state_value):
        if isinstance(state_value, dict):
            if 'leds' in state_value and len(state_value['leds']) > 0:
                return state_value['leds'][0].get('state')
            if 'state' in state_value:
                return state_value['state']
        return None

    def _should_publish_state(self, device_id, resource, state_value):
        key = (device_id, resource)
        if key in self.recent_commands:
            cmd_time, expected_state = self.recent_commands[key]
            elapsed = time.time() - cmd_time

            if elapsed < self.command_suppress_time:
                actual_state = self._extract_led_state(state_value)

                if actual_state == expected_state:
                    logger.info(f"Device confirmed state {expected_state} for {device_id}/{resource}")
                    del self.recent_commands[key]
                    return True

                logger.debug(
                    "Suppressing poll update for %s/%s (expected=%s, actual=%s, elapsed=%.1fs)",
                    device_id,
                    resource,
                    expected_state,
                    actual_state,
                    elapsed,
                )
                return False

            logger.info(f"Command suppression expired for {device_id}/{resource}, publishing real state")
            del self.recent_commands[key]
            return True

        return True

    def _translate_mqtt_to_coap(self, resource, mqtt_payload):
        try:
            if resource == 'led':
                state_str = mqtt_payload.strip().upper()
                if state_str == 'ON':
                    state_num = 1
                elif state_str == 'OFF':
                    state_num = 0
                elif state_str == 'TOGGLE':
                    state_num = 2
                else:
                    logger.warning(f"Unknown LED state: {state_str}")
                    return None

                return {'led_id': 0, 'state': state_num}

            try:
                return json.loads(mqtt_payload)
            except json.JSONDecodeError:
                return mqtt_payload

        except Exception as e:
            logger.error(f"Error translating MQTT to CoAP: {e}")
            return None

    async def _handle_mqtt_command(self, device_id, resource, payload):
        logger.info(f"Received MQTT command: {device_id}/{resource} = {payload}")

        try:
            device = await self.registry.get_device_by_id(device_id)
            if not device:
                logger.warning(f"Device {device_id} not found in registry")
                return

            if resource == 'auth_request':
                await self._handle_auth_request(device)
                return

            uri_path = f"/{resource}"
            coap_payload = self._translate_mqtt_to_coap(resource, payload)
            if coap_payload is None:
                logger.warning(f"Could not translate MQTT payload: {payload}")
                return

            if resource == 'led' and isinstance(coap_payload, dict) and 'state' in coap_payload:
                expected_state = coap_payload['state']
                self.recent_commands[(device_id, resource)] = (time.time(), expected_state)
                self.mqtt.publish_state(device_id, uri_path, {'state': expected_state})
                logger.info(f"Published optimistic state for {device_id}/{resource}: {expected_state}")

            success = await self.coap_client.put_resource(device.ipv6_address, uri_path, coap_payload)
            if success:
                logger.info(f"Successfully sent command to {device_id}{uri_path}")
            else:
                logger.warning(f"Failed to send command to {device_id}{uri_path}")
                self.recent_commands.pop((device_id, resource), None)

        except Exception as e:
            logger.error(f"Error handling MQTT command: {e}")

    async def _cleanup(self):
        logger.info("Cleaning up...")

        for task in self.background_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        for (device_id, source_uri) in list(self.resource_tasks.keys()):
            await self._cancel_resource_task(device_id, source_uri)

        for device_id, task in list(self.auth_expiry_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.auth_expiry_tasks.pop(device_id, None)

        if self.coap_client:
            await self.coap_client.shutdown()

        if self.discovery:
            await self.discovery.shutdown()

        if self.mqtt:
            await self.mqtt.disconnect()

        if self.registry:
            await self.registry.close()

        logger.info("Cleanup complete")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )

    try:
        logger.info("Initializing Thread CoAP Bridge...")
        service = CoAPBridgeService()
        asyncio.run(service.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
