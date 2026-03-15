import asyncio

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
