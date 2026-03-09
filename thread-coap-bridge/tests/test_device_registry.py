import asyncio

from device_registry import DeviceRegistry


def run(coro):
    return asyncio.run(coro)


def sample_resources(*extra):
    base = [
        {"uri_path": "/led", "resource_type": "led", "observable": True},
        {"uri_path": "/uptime", "resource_type": "uptime", "observable": False},
    ]
    return base + list(extra)


def test_register_device_marks_changed_resources_uncommissioned(tmp_path):
    db_path = tmp_path / "devices.db"

    async def scenario():
        registry = DeviceRegistry(db_path=str(db_path))
        await registry.initialize()

        first = await registry.register_device("fd00::1234", resources=sample_resources())
        assert first.is_new is True
        assert first.resources_changed is True
        assert first.commissioned is False

        await registry.mark_commissioned(first.device_id)

        unchanged = await registry.register_device("fd00::1234", resources=sample_resources())
        assert unchanged.is_new is False
        assert unchanged.resources_changed is False
        assert unchanged.commissioned is True

        changed = await registry.register_device(
            "fd00::1234",
            resources=sample_resources({"uri_path": "/auth", "resource_type": "auth", "observable": False}),
        )
        assert changed.is_new is False
        assert changed.resources_changed is True
        assert changed.commissioned is False

        resources = await registry.get_device_resources(changed.device_id)
        assert [resource.uri_path for resource in resources] == ["/auth", "/led", "/uptime"]
        await registry.close()

    run(scenario())


def test_offline_rediscovery_requeues_runtime_even_without_capability_delta(tmp_path):
    db_path = tmp_path / "devices.db"

    async def scenario():
        registry = DeviceRegistry(db_path=str(db_path))
        await registry.initialize()

        first = await registry.register_device("fd00::5678", resources=sample_resources())
        await registry.mark_commissioned(first.device_id)
        await registry.mark_device_offline(first.device_id)

        rediscovered = await registry.register_device("fd00::5678", resources=sample_resources())
        assert rediscovered.resources_changed is False
        assert rediscovered.commissioned is False

        uncommissioned = await registry.get_uncommissioned_devices()
        assert [device.device_id for device in uncommissioned] == [first.device_id]
        await registry.close()

    run(scenario())
