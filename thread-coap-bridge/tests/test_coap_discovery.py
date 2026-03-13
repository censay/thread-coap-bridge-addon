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


def test_interface_candidate_parsers_filter_local_and_non_global_addresses():
    discovery = CoAPDiscovery(None, {"thread_interface": "wpan0"})

    addr_output = """
3: wpan0    inet6 fd35:5807:223f:1:426e:bdbf:1ec3:ee80/64 scope global
   valid_lft forever preferred_lft forever
   inet6 fe80::402d:1f04:e15e:9e9/64 scope link
   valid_lft forever preferred_lft forever
"""
    neigh_output = """
fd35:5807:223f:1:235e:586d:bd3b:4921 dev wpan0 lladdr 66:55:44:33:22:11 REACHABLE
fe80::9c8e:c2c6:4c17:4f4b dev wpan0 lladdr 66:55:44:33:22:11 STALE
"""
    route_output = """
fd35:5807:223f:1:235e:586d:bd3b:4921/128 dev wpan0 metric 1024 pref medium
fd35:5807:223f:1::/64 dev wpan0 metric 256 pref medium
default via fe80::1 dev wpan0 metric 1024 pref medium
"""

    local_addrs = discovery._extract_interface_addresses(addr_output)
    neighbor_candidates = discovery._extract_neighbor_candidates(neigh_output)
    route_candidates = discovery._extract_route_candidates(route_output)

    assert local_addrs == {"fd35:5807:223f:1:426e:bdbf:1ec3:ee80"}
    assert neighbor_candidates == {"fd35:5807:223f:1:235e:586d:bd3b:4921"}
    assert route_candidates == {"fd35:5807:223f:1:235e:586d:bd3b:4921"}


def test_interface_candidates_are_probed_before_multicast():
    class FakeRegistry:
        def __init__(self):
            self.calls = []

        async def register_device(self, ipv6_addr, resources=None):
            self.calls.append((ipv6_addr, resources))
            return {"device_id": "thread_iface", "ipv6_addr": ipv6_addr}

    async def scenario():
        registry = FakeRegistry()
        discovery = CoAPDiscovery(registry, {"thread_interface": "wpan0"})
        discovery.context = object()
        discovery.start_cycle()

        async def fake_collect():
            return ["fd35:5807:223f:1:235e:586d:bd3b:4921"]

        async def fake_query(ipv6_addr, timeout=65.0):
            assert timeout == 65.0
            assert ipv6_addr == "fd35:5807:223f:1:235e:586d:bd3b:4921"
            return [{"uri_path": "/uptime", "resource_type": "uptime", "observable": False}]

        discovery._collect_interface_candidates = fake_collect
        discovery.query_device_resources = fake_query

        results = await discovery.discover_interface_devices()

        assert registry.calls == [
            (
                "fd35:5807:223f:1:235e:586d:bd3b:4921",
                [{"uri_path": "/uptime", "resource_type": "uptime", "observable": False}],
            )
        ]
        assert results == [{"device_id": "thread_iface", "ipv6_addr": "fd35:5807:223f:1:235e:586d:bd3b:4921"}]

    run(scenario())
