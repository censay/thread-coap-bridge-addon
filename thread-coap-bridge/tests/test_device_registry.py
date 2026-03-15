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
        assert first.needs_runtime_reconcile is True
        assert first.commissioned is False

        await registry.mark_commissioned(first.device_id)

        unchanged = await registry.register_device("fd00::1234", resources=sample_resources())
        assert unchanged.is_new is False
        assert unchanged.resources_changed is False
        assert unchanged.needs_runtime_reconcile is False
        assert unchanged.commissioned is True

        changed = await registry.register_device(
            "fd00::1234",
            resources=sample_resources({"uri_path": "/auth", "resource_type": "auth", "observable": False}),
        )
        assert changed.is_new is False
        assert changed.resources_changed is True
        assert changed.needs_runtime_reconcile is True
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
        assert rediscovered.needs_runtime_reconcile is True
        assert rediscovered.commissioned is False

        uncommissioned = await registry.get_uncommissioned_devices()
        assert [device.device_id for device in uncommissioned] == [first.device_id]
        await registry.close()

    run(scenario())


def test_duplicate_announce_before_commission_does_not_requeue_runtime(tmp_path):
    db_path = tmp_path / "devices.db"

    async def scenario():
        registry = DeviceRegistry(db_path=str(db_path))
        await registry.initialize()

        first = await registry.register_device("fd00::9999", resources=sample_resources())
        duplicate = await registry.register_device("fd00::9999", resources=sample_resources())

        assert first.needs_runtime_reconcile is True
        assert duplicate.resources_changed is False
        assert duplicate.needs_runtime_reconcile is False
        assert duplicate.commissioned is False
        await registry.close()

    run(scenario())


def test_mark_all_devices_offline_resets_online_state(tmp_path):
    db_path = tmp_path / "devices.db"

    async def scenario():
        registry = DeviceRegistry(db_path=str(db_path))
        await registry.initialize()

        first = await registry.register_device("fd00::1111", resources=sample_resources())
        second = await registry.register_device("fd00::2222", resources=sample_resources())

        await registry.mark_commissioned(first.device_id)
        await registry.mark_commissioned(second.device_id)
        await registry.mark_all_devices_offline()

        devices = await registry.get_all_devices()
        assert {device.device_id: device.is_online for device in devices} == {
            first.device_id: False,
            second.device_id: False,
        }
        await registry.close()

    run(scenario())
