import asyncio
import json

from resource_handlers import ResourceHandlerRegistry, ResourceRecord


def run(coro):
    return asyncio.run(coro)


class StubCoAPClient:
    async def get_resource(self, ipv6_addr, uri_path):
        assert ipv6_addr == "fd00::1"
        assert uri_path == "/sw"
        return json.dumps(
            {
                "btns": [
                    {"btn_id": 0, "state": 0},
                    {"btn_id": 1, "state": 1},
                ]
            }
        )


class StubDevice:
    device_id = "thread_test"
    ipv6_address = "fd00::1"


def test_auth_resource_expands_to_sensor_and_button_entities():
    registry = ResourceHandlerRegistry()

    entities = run(
        registry.build_entities(
            StubCoAPClient(),
            StubDevice(),
            ResourceRecord(uri_path="/auth", resource_type="auth"),
        )
    )

    assert [entity.object_id for entity in entities] == ["auth_tier", "auth_request"]
    assert {entity.component for entity in entities} == {"sensor", "button"}
    assert registry.build_monitor_specs({entity.object_id: entity for entity in entities}) == {}


def test_button_resource_expands_to_per_button_entities():
    registry = ResourceHandlerRegistry()

    entities = run(
        registry.build_entities(
            StubCoAPClient(),
            StubDevice(),
            ResourceRecord(uri_path="/sw", resource_type="button", observable=True),
        )
    )

    assert [entity.object_id for entity in entities] == ["sw0", "sw1"]
    monitors = registry.build_monitor_specs({entity.object_id: entity for entity in entities})
    assert list(monitors.keys()) == ["/sw"]
