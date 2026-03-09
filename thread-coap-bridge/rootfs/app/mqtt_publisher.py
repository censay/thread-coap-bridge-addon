"""
MQTT Publisher Module

Handles Home Assistant MQTT Discovery and state publishing.
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTPublisher:
    """MQTT publisher for Home Assistant integration."""

    def __init__(self, mqtt_config):
        self.host = mqtt_config['host']
        self.port = mqtt_config['port']
        self.username = mqtt_config.get('username', '')
        self.password = mqtt_config.get('password', '')
        self.client = None
        self.discovery_prefix = "homeassistant"
        self.connected = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.loop = None

        logger.info(f"MQTT Publisher initialized (broker: {self.host}:{self.port})")

    async def connect(self):
        logger.info(f"Connecting to MQTT broker at {self.host}:{self.port}")

        try:
            self.loop = asyncio.get_event_loop()

            import time
            client_id = f"thread_coap_bridge_{int(time.time())}"
            self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
            logger.info(f"MQTT client ID: {client_id}")

            if self.username and self.username.strip():
                logger.info(f"Setting MQTT credentials for user: {self.username}")
                self.client.username_pw_set(self.username, self.password)
            else:
                logger.info("Connecting to MQTT without authentication")

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message

            await asyncio.get_event_loop().run_in_executor(
                self.executor,
                lambda: self.client.connect(self.host, self.port, keepalive=60),
            )

            self.client.loop_start()
            await asyncio.sleep(2)

            if self.connected:
                logger.info("Successfully connected to MQTT broker")
            else:
                logger.warning("MQTT connection status uncertain")

            self.client.subscribe("thread/+/+/set")
            logger.info("Subscribed to command topics: thread/+/+/set")

        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            raise

    async def disconnect(self):
        logger.info("Disconnecting from MQTT broker")

        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False

        self.executor.shutdown(wait=True)
        logger.info("MQTT client disconnected")

    def publish_discovery(self, device_id, resource_type, resource_uri, ipv6_addr):
        object_id = resource_uri.strip('/')
        component = self._map_resource_to_component(resource_type)
        entity = type('Entity', (), {
            'object_id': object_id,
            'component': component,
            'resource_type': resource_type,
            'state_uri': resource_uri,
            'sensor_availability': resource_type.lower() in ("battery", "voltage", "uptime"),
            'command_resource': object_id if component in ('light', 'button') else None,
        })
        self.publish_entity_discovery(device_id, entity, ipv6_addr)

    def publish_entity_discovery(self, device_id, entity, ipv6_addr):
        component = entity.component
        object_id = entity.object_id
        topic = f"{self.discovery_prefix}/{component}/{device_id}/{object_id}/config"
        availability_topic = f"thread/{device_id}/availability"

        payload = {
            "name": f"{device_id} {object_id}",
            "unique_id": f"{device_id}_{object_id}",
            "availability_topic": availability_topic,
            "device": {
                "identifiers": [device_id],
                "name": f"Thread Device {device_id}",
                "manufacturer": "Thread CoAP Device",
                "model": "nRF54L15",
                "sw_version": "1.0.0",
            },
        }

        if component != 'button':
            state_topic = f"thread/{device_id}/{object_id}/state"
            payload["state_topic"] = state_topic

        resource_lower = entity.resource_type.lower()

        if component == "light":
            command_resource = entity.command_resource or object_id
            payload["command_topic"] = f"thread/{device_id}/{command_resource}/set"
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
            payload["state_value_template"] = "{{ 'ON' if value == '1' else 'OFF' }}"
            payload["optimistic"] = False

        elif component == "binary_sensor":
            payload["payload_on"] = "1"
            payload["payload_off"] = "0"

        elif component == "button":
            command_resource = entity.command_resource or object_id
            payload["command_topic"] = f"thread/{device_id}/{command_resource}/set"
            payload["payload_press"] = "PRESS"
            payload["entity_category"] = "config"

        elif component == "sensor":
            unit = self._get_unit_for_sensor(resource_lower)
            if unit:
                payload["unit_of_measurement"] = unit

            if resource_lower == "battery":
                payload["device_class"] = "battery"
            elif resource_lower == "uptime":
                payload["device_class"] = "duration"
                payload["state_class"] = "total_increasing"
            elif resource_lower == "auth_tier":
                payload["icon"] = "mdi:shield-key"

            if getattr(entity, 'sensor_availability', False):
                payload["availability_topic"] = f"thread/{device_id}/{object_id}/availability"

        payload_json = json.dumps(payload)
        logger.info(f"Publishing discovery: {device_id}/{object_id} ({component})")

        try:
            result = self.client.publish(topic, payload_json, qos=1, retain=True)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(f"Failed to queue discovery: {result.rc}")
                return

            result.wait_for_publish(timeout=5.0)
            if result.is_published():
                logger.info(f"Published discovery to {topic}")
            else:
                logger.warning(f"Discovery to {topic} may not have been delivered")

        except Exception as e:
            logger.error(f"Exception publishing discovery to {topic}: {e}")

    def publish_discovery_removal(self, component, device_id, object_id):
        topic = f"{self.discovery_prefix}/{component}/{device_id}/{object_id}/config"
        try:
            result = self.client.publish(topic, "", qos=1, retain=True)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(f"Failed to publish removal for {topic}: {result.rc}")
        except Exception as exc:
            logger.error(f"Exception removing discovery topic {topic}: {exc}")

    def publish_state(self, device_id, resource_uri, state_value):
        object_id = resource_uri.strip('/')
        logger.info(
            "publish_state called: device=%s, uri=%s, value=%s (type=%s)",
            device_id,
            resource_uri,
            state_value,
            type(state_value).__name__,
        )

        if isinstance(state_value, dict):
            if 'btns' in state_value and len(state_value['btns']) > 0:
                base_object_id = object_id
                for btn in state_value['btns']:
                    btn_id = btn.get('btn_id', 0)
                    btn_state = str(btn.get('state', 0))
                    btn_topic = f"thread/{device_id}/{base_object_id}{btn_id}/state"
                    result = self.client.publish(btn_topic, btn_state, qos=1, retain=False)
                    if result.rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.error(f"Failed to publish button state: {result.rc}")
                return
            if 'leds' in state_value and len(state_value['leds']) > 0:
                payload = str(state_value['leds'][0].get('state', 0))
            elif 'state' in state_value:
                payload = str(state_value['state'])
            elif 'value' in state_value:
                payload = str(state_value['value'])
            else:
                payload = json.dumps(state_value, sort_keys=True)
        else:
            payload = str(state_value)

        state_topic = f"thread/{device_id}/{object_id}/state"

        if not self.connected:
            logger.error(f"MQTT not connected! Cannot publish to {state_topic}")
            return

        logger.info(f"Publishing state: {state_topic} = {payload}")
        try:
            result = self.client.publish(state_topic, payload, qos=1, retain=True)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(f"Failed to queue publish to {state_topic}: rc={result.rc}")
                return

            result.wait_for_publish(timeout=5.0)
            if not result.is_published():
                logger.warning(f"Message to {state_topic} may not have been delivered")
        except Exception as e:
            logger.error(f"Exception publishing to {state_topic}: {e}")

    def publish_availability(self, device_id, available=True):
        avail_topic = f"thread/{device_id}/availability"
        payload = "online" if available else "offline"
        logger.info(f"Publishing availability: {device_id} = {payload}")
        result = self.client.publish(avail_topic, payload, qos=1, retain=True)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(f"Failed to publish availability: {result.rc}")

    def publish_sensor_availability(self, device_id, sensor_type, available=True):
        avail_topic = f"thread/{device_id}/{sensor_type}/availability"
        payload = "online" if available else "offline"
        logger.info(f"Publishing sensor availability: {device_id}/{sensor_type} = {payload}")
        result = self.client.publish(avail_topic, payload, qos=1, retain=True)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(f"Failed to publish sensor availability: {result.rc}")

    def set_command_callback(self, callback):
        self._command_callback = callback

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connection established")
            self.connected = True
        else:
            logger.error(f"MQTT connection failed with code {rc}")
            self.connected = False

    def _on_disconnect(self, client, userdata, rc):
        logger.warning(f"MQTT disconnected with code {rc}")
        self.connected = False

    def _on_message(self, client, userdata, msg):
        logger.debug(f"Received MQTT message: {msg.topic} = {msg.payload}")

        try:
            parts = msg.topic.split('/')
            if len(parts) >= 4 and parts[0] == 'thread' and parts[3] == 'set':
                device_id = parts[1]
                resource = parts[2]
                payload = msg.payload.decode('utf-8')

                if hasattr(self, '_command_callback') and self._command_callback and self.loop:
                    asyncio.run_coroutine_threadsafe(
                        self._command_callback(device_id, resource, payload),
                        self.loop,
                    )

        except Exception as e:
            logger.error(f"Error processing MQTT command: {e}")

    def _map_resource_to_component(self, resource_type):
        mapping = {
            "light": "light",
            "led": "light",
            "switch": "switch",
            "button": "binary_sensor",
            "battery": "sensor",
            "temperature": "sensor",
            "humidity": "sensor",
            "uptime": "sensor",
            "voltage": "sensor",
            "auth": "sensor",
            "auth_tier": "sensor",
            "auth_request": "button",
        }
        return mapping.get(resource_type.lower(), "sensor")

    def _get_unit_for_sensor(self, resource_type):
        units = {
            "temperature": "°C",
            "humidity": "%",
            "battery": "%",
            "voltage": "V",
            "current": "A",
            "uptime": "s",
        }
        return units.get(resource_type.lower(), None)
