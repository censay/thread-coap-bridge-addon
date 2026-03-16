import asyncio

import coap_client as coap_client_module
from aiocoap import NON

from coap_client import CoAPClient
from coap_discovery import CoAPDiscovery


def run(coro):
    return asyncio.run(coro)


class FakeCode:
    def is_successful(self):
        return True


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.code = FakeCode()


class FakePendingRequest:
    def __init__(self, response):
        self.response = asyncio.sleep(0, result=response)


class FakeContext:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return FakePendingRequest(self.response)


def test_get_resource_uses_non_confirmable_reads():
    async def scenario():
        mqtt = type("FakeMQTT", (), {})()
        client = CoAPClient(mqtt)
        client.context = FakeContext(FakeResponse(b'{"value": 42}'))

        payload = await client.get_resource("fd00::1234", "/battery")

        assert payload == '{"value": 42}'
        assert client.context.requests[0].mtype == NON

    run(scenario())


def test_query_device_resources_uses_non_confirmable_reads():
    async def scenario():
        discovery = CoAPDiscovery(None, {})
        discovery.context = FakeContext(FakeResponse(b'</auth>;rt="auth"'))

        resources = await discovery.query_device_resources("fd00::1234")

        assert resources == [
            {
                "uri_path": "/auth",
                "resource_type": "auth",
                "interface_type": None,
                "observable": False,
            }
        ]
        assert discovery.context.requests[0].mtype == NON

    run(scenario())


def test_observe_registration_timeout_does_not_mark_device_offline():
    class FakeObservation:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeObserveRequest:
        def __init__(self):
            self.response = asyncio.Future()
            self.observation = FakeObservation()

    class FakeObserveContext:
        def __init__(self):
            self.requests = []

        def request(self, request):
            self.requests.append(request)
            return FakeObserveRequest()

    class FakeMQTT:
        def __init__(self):
            self.availability = []

        def publish_availability(self, device_id, available):
            self.availability.append((device_id, available))

        def publish_state(self, device_id, uri_path, payload):
            pass

    class FakeRegistry:
        def __init__(self):
            self.offline_calls = []

        async def mark_device_offline(self, device_id):
            self.offline_calls.append(device_id)

        async def update_device_failure(self, device_id, failed=True):
            pass

    async def scenario():
        mqtt = FakeMQTT()
        registry = FakeRegistry()
        client = CoAPClient(mqtt)
        client.context = FakeObserveContext()

        original_timeout = coap_client_module.OBSERVE_REGISTRATION_TIMEOUT
        original_retry = coap_client_module.MAX_RETRY_DELAY
        coap_client_module.OBSERVE_REGISTRATION_TIMEOUT = 0.01
        coap_client_module.MAX_RETRY_DELAY = 0.01

        task = asyncio.create_task(
            client.observe_resource(
                "thread_dev",
                "fd00::1234",
                "/sw",
                registry=registry,
                offline_threshold=1,
            )
        )

        await asyncio.sleep(0.05)
        client.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            coap_client_module.OBSERVE_REGISTRATION_TIMEOUT = original_timeout
            coap_client_module.MAX_RETRY_DELAY = original_retry

        assert mqtt.availability == []
        assert registry.offline_calls == []

    run(scenario())
