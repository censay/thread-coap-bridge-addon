import asyncio

from coap_discovery import CoAPDiscovery


def run(coro):
    return asyncio.run(coro)


def test_core_link_parser_tracks_obs_per_resource():
    discovery = CoAPDiscovery(None, {})

    resources = discovery._parse_core_link_format(
        '</led>;rt="led";obs,</battery>;rt="battery",</auth>;rt="auth";if="rw"'
    )

    assert resources == [
        {
            "uri_path": "/led",
            "resource_type": "led",
            "interface_type": None,
            "observable": True,
        },
        {
            "uri_path": "/battery",
            "resource_type": "battery",
            "interface_type": None,
            "observable": False,
        },
        {
            "uri_path": "/auth",
            "resource_type": "auth",
            "interface_type": "rw",
            "observable": False,
        },
    ]


def test_seed_addresses_are_normalized_and_probed_via_unicast():
    class FakeRegistry:
        def __init__(self):
            self.calls = []

        async def register_device(self, ipv6_addr, resources=None):
            self.calls.append((ipv6_addr, resources))
            return {"device_id": "thread_seed", "ipv6_addr": ipv6_addr}

    async def scenario():
        registry = FakeRegistry()
        discovery = CoAPDiscovery(
            registry,
            {
                "seed_ipv6_addresses": [
                    " [fd00::1234] ",
                    "fd00::1234%wpan0",
                    "not-an-ipv6-address",
                    "fd00::5678",
                ]
            },
        )
        discovery.context = object()
        discovery.start_cycle()

        queried = []

        async def fake_query(ipv6_addr, timeout=65.0):
            queried.append((ipv6_addr, timeout))
            if ipv6_addr == "fd00::1234":
                return [{"uri_path": "/auth", "resource_type": "auth", "observable": False}]
            return []

        discovery.query_device_resources = fake_query

        results = await discovery.discover_seed_devices()

        assert discovery.seed_addresses == ["fd00::1234", "fd00::5678"]
        assert queried == [("fd00::1234", 65.0), ("fd00::5678", 65.0)]
        assert registry.calls == [
            (
                "fd00::1234",
                [{"uri_path": "/auth", "resource_type": "auth", "observable": False}],
            )
        ]
        assert results == [{"device_id": "thread_seed", "ipv6_addr": "fd00::1234"}]

    run(scenario())
