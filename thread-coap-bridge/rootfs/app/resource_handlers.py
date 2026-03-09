"""
Resource handler registry for CoAP resources.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResourceRecord:
    uri_path: str
    resource_type: str
    interface_type: Optional[str] = None
    observable: bool = False


@dataclass(frozen=True)
class EntitySpec:
    object_id: str
    component: str
    resource_type: str
    state_uri: Optional[str]
    source_uri: Optional[str]
    monitor_mode: Optional[str] = None
    poll_interval: int = 120
    initial_delay: int = 0
    sensor_availability: bool = False
    command_resource: Optional[str] = None

    @property
    def monitor_key(self):
        if not self.monitor_mode or not self.source_uri:
            return None
        return self.source_uri


class ResourceHandlerRegistry:
    """Resolve CoAP resources into Home Assistant entities."""

    async def build_entities(self, coap_client, device, resource: ResourceRecord) -> List[EntitySpec]:
        resource_type = (resource.resource_type or "unknown").lower()

        if resource_type == "led":
            return [
                EntitySpec(
                    object_id=resource.uri_path.strip("/"),
                    component="light",
                    resource_type="led",
                    state_uri=resource.uri_path,
                    source_uri=resource.uri_path,
                    monitor_mode="observe",
                    command_resource=resource.uri_path.strip("/"),
                )
            ]

        if resource_type == "button":
            return await self._build_button_entities(coap_client, device, resource)

        if resource_type == "battery":
            return [
                EntitySpec(
                    object_id="battery",
                    component="sensor",
                    resource_type="battery",
                    state_uri=resource.uri_path,
                    source_uri=resource.uri_path,
                    monitor_mode="poll",
                    poll_interval=120,
                    initial_delay=0,
                    sensor_availability=True,
                )
            ]

        if resource_type == "voltage":
            return [
                EntitySpec(
                    object_id="voltage",
                    component="sensor",
                    resource_type="voltage",
                    state_uri=resource.uri_path,
                    source_uri=resource.uri_path,
                    monitor_mode="poll",
                    poll_interval=120,
                    initial_delay=40,
                    sensor_availability=True,
                )
            ]

        if resource_type == "uptime":
            return [
                EntitySpec(
                    object_id="uptime",
                    component="sensor",
                    resource_type="uptime",
                    state_uri=resource.uri_path,
                    source_uri=resource.uri_path,
                    monitor_mode="poll",
                    poll_interval=120,
                    initial_delay=80,
                    sensor_availability=True,
                )
            ]

        if resource_type == "auth":
            return [
                EntitySpec(
                    object_id="auth_tier",
                    component="sensor",
                    resource_type="auth_tier",
                    state_uri="/auth_tier",
                    source_uri=None,
                    sensor_availability=False,
                ),
                EntitySpec(
                    object_id="auth_request",
                    component="button",
                    resource_type="auth_request",
                    state_uri=None,
                    source_uri=None,
                    command_resource="auth_request",
                ),
            ]

        base_object_id = resource.uri_path.strip("/") or "resource"
        return [
            EntitySpec(
                object_id=base_object_id,
                component="sensor",
                resource_type=resource_type,
                state_uri=resource.uri_path,
                source_uri=resource.uri_path,
                monitor_mode="poll",
                poll_interval=120,
                initial_delay=0,
                sensor_availability=True,
            )
        ]

    async def _build_button_entities(self, coap_client, device, resource: ResourceRecord) -> List[EntitySpec]:
        base_object_id = resource.uri_path.strip("/") or "button"
        entities: List[EntitySpec] = []

        try:
            payload = await coap_client.get_resource(device.ipv6_address, resource.uri_path)
            if payload:
                data = json.loads(payload)
                btns = data.get("btns", [])
                for btn in btns:
                    btn_id = btn.get("btn_id", 0)
                    entities.append(
                        EntitySpec(
                            object_id=f"{base_object_id}{btn_id}",
                            component="binary_sensor",
                            resource_type="button",
                            state_uri=f"/{base_object_id}{btn_id}",
                            source_uri=resource.uri_path,
                            monitor_mode="observe",
                        )
                    )
        except Exception as exc:
            logger.warning("Could not resolve button count for %s: %s", device.device_id, exc)

        if entities:
            return entities

        return [
            EntitySpec(
                object_id=base_object_id,
                component="binary_sensor",
                resource_type="button",
                state_uri=resource.uri_path,
                source_uri=resource.uri_path,
                monitor_mode="observe",
            )
        ]

    def build_monitor_specs(self, entities: Dict[str, EntitySpec]) -> Dict[str, EntitySpec]:
        monitors: Dict[str, EntitySpec] = {}
        for entity in entities.values():
            if entity.monitor_key and entity.monitor_key not in monitors:
                monitors[entity.monitor_key] = entity
        return monitors
