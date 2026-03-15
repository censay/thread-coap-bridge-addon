import asyncio

from coap_discovery import CoAPDiscovery, HttpResponse


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


def test_process_announcement_registers_device_with_announced_eui64():
    class FakeRegistry:
        def __init__(self):
            self.calls = []

        async def register_device(self, ipv6_addr, eui64=None, resources=None):
            self.calls.append((ipv6_addr, eui64, resources))
            return {"device_id": eui64 or "thread_dev", "ipv6_addr": ipv6_addr}

    async def scenario():
        registry = FakeRegistry()
        discovery = CoAPDiscovery(registry, {})
        discovery.context = object()

        async def fake_query(ipv6_addr, timeout=65.0):
            assert ipv6_addr == "fd35:5807:223f:1:235e:586d:bd3b:4921"
            assert timeout == 65.0
            return [{"uri_path": "/uptime", "resource_type": "uptime", "observable": False}]

        discovery.query_device_resources = fake_query

        result = await discovery.process_announcement(
            "fd35:5807:223f:1:235e:586d:bd3b:4921",
            eui64="6234567890aacdea",
        )

        assert registry.calls == [
            (
                "fd35:5807:223f:1:235e:586d:bd3b:4921",
                "6234567890aacdea",
                [{"uri_path": "/uptime", "resource_type": "uptime", "observable": False}],
            )
        ]
        assert result == {
            "device_id": "6234567890aacdea",
            "ipv6_addr": "fd35:5807:223f:1:235e:586d:bd3b:4921",
        }

    run(scenario())


def test_otbr_device_payload_extracts_omr_candidates_and_eui64():
    discovery = CoAPDiscovery(None, {"otbr_rest_url": "http://172.30.32.1:8081"})

    devices = [
        {
            "id": "96518e5497d5b9f3",
            "type": "threadBorderRouter",
            "attributes": {
                "extAddress": "96518e5497d5b9f3",
                "omrIpv6Address": ["fd35:5807:223f:1:426e:bdbf:1ec3:ee80"],
                "role": "leader",
            },
        },
        {
            "id": "de62e016db392476",
            "type": "threadDevice",
            "attributes": {
                "extAddress": "de62e016db392476",
                "omrIpv6Address": ["fd35:5807:223f:1:235e:586d:bd3b:4921"],
                "eui64": "62:34:56:78:90:aa:cd:ea",
                "role": "child",
            },
        },
        {
            "id": "de62e016db392477",
            "attributes": {
                "extAddress": "de62e016db392477",
                "omrIpv6Address": [
                    "fd35:5807:223f:1:235e:586d:bd3b:4921",
                    "fe80::1",
                    "fd35:5807:223f:1:1111:2222:3333:4444",
                ],
                "eui": "90:35:ea:ff:fe:f3:e0:9c",
                "role": "router",
            },
        },
    ]

    candidates = discovery._extract_otbr_candidates(devices)

    assert candidates == [
        {
            "device_id": "de62e016db392476",
            "ipv6_address": "fd35:5807:223f:1:235e:586d:bd3b:4921",
            "eui64": "62:34:56:78:90:aa:cd:ea",
            "role": "child",
        },
        {
            "device_id": "de62e016db392477",
            "ipv6_address": "fd35:5807:223f:1:1111:2222:3333:4444",
            "eui64": "90:35:ea:ff:fe:f3:e0:9c",
            "role": "router",
        },
    ]


def test_otbr_base_url_normalizer_strips_optional_api_suffix():
    discovery = CoAPDiscovery(None, {})

    assert discovery._normalize_otbr_base_url("http://127.0.0.1:8081/api") == "http://127.0.0.1:8081"
    assert discovery._normalize_otbr_base_url(" http://172.30.32.1:8081/ ") == "http://172.30.32.1:8081"
    assert discovery._normalize_otbr_base_url("") is None


def test_resolve_otbr_addon_slug_prefers_direct_core_slug_probe():
    async def scenario():
        discovery = CoAPDiscovery(None, {})
        discovery.supervisor_token = "token"

        async def fake_supervisor_request(method, path, payload=None):
            assert method == "GET"
            if path == "/addons/core_openthread_border_router/info":
                return HttpResponse(200, "", {"data": {"slug": "core_openthread_border_router"}})
            raise AssertionError(path)

        discovery._supervisor_request = fake_supervisor_request

        assert await discovery._resolve_otbr_addon_slug() == "core_openthread_border_router"

    run(scenario())


def test_resolve_otbr_base_url_uses_supervisor_ip_address():
    async def scenario():
        discovery = CoAPDiscovery(None, {})
        discovery.supervisor_token = "token"

        async def fake_supervisor_request(method, path, payload=None):
            if path == "/addons/core_openthread_border_router/info":
                return HttpResponse(
                    200,
                    "",
                    {
                        "data": {
                            "slug": "core_openthread_border_router",
                            "ip_address": "172.30.32.1",
                            "hostname": "core-openthread-border-router",
                        }
                    },
                )

            raise AssertionError(path)

        discovery._supervisor_request = fake_supervisor_request

        assert await discovery._resolve_otbr_base_url() == "http://172.30.32.1:8081"

    run(scenario())


def test_resolve_otbr_addon_slug_falls_back_to_addon_listing_when_direct_probe_misses():
    async def scenario():
        discovery = CoAPDiscovery(None, {})
        discovery.supervisor_token = "token"

        async def fake_supervisor_request(method, path, payload=None):
            if path == "/addons/core_openthread_border_router/info":
                return HttpResponse(404, "", {"message": "missing"})
            if path == "/addons/local_openthread_border_router/info":
                return HttpResponse(404, "", {"message": "missing"})
            if path == "/addons":
                return HttpResponse(
                    200,
                    "",
                    {
                        "data": {
                            "addons": [
                                {"slug": "custom_repo_openthread_border_router", "name": "OpenThread Border Router"},
                            ]
                        }
                    },
                )
            raise AssertionError(path)

        discovery._supervisor_request = fake_supervisor_request

        assert await discovery._resolve_otbr_addon_slug() == "custom_repo_openthread_border_router"

    run(scenario())


def test_otbr_inventory_is_disabled_after_http_404():
    async def scenario():
        discovery = CoAPDiscovery(None, {"otbr_rest_url": "http://172.30.32.1:8081"})
        discovery.context = object()
        calls = []

        async def fake_http_request(method, url, headers=None, payload=None):
            calls.append(url)
            if url == "http://172.30.32.1:8081/api/devices":
                return HttpResponse(404, '{"ErrorCode":404}', {"ErrorCode": 404})
            return HttpResponse(200, "{}", {})

        discovery._http_request = fake_http_request
        discovery._collect_local_interface_addresses = lambda: asyncio.sleep(0, result=set())

        assert await discovery.discover_otbr_devices() == []
        assert discovery.otbr_inventory_supported is False
        assert calls == [
            "http://172.30.32.1:8081/api/devices",
            "http://172.30.32.1:8081/",
            "http://172.30.32.1:8081/get_properties",
            "http://172.30.32.1:8081/api/node",
            "http://172.30.32.1:8081/node",
            "http://172.30.32.1:8081/node",
        ]

    run(scenario())


def test_otbr_node_payload_extracts_only_non_local_ipv6_candidates():
    discovery = CoAPDiscovery(None, {})

    payload = {
        "NetworkName": "OpenThread",
        "Rloc": "fd30:f978:c1a3:183c:0:ff:fe00:fc11",
        "MeshLocalAddress": "fdde:ad00:beef:0:235e:586d:bd3b:4921",
        "Addresses": [
            "fd35:5807:223f:1:426e:bdbf:1ec3:ee80",
            "fd35:5807:223f:1:235e:586d:bd3b:4921",
            "fe80::1",
        ],
    }

    candidates = discovery._extract_otbr_node_candidates(
        payload,
        local_addrs={"fd35:5807:223f:1:426e:bdbf:1ec3:ee80"},
    )

    assert candidates == [
        {
            "device_id": "node_candidate_1",
            "ipv6_address": "fd30:f978:c1a3:183c:0:ff:fe00:fc11",
            "eui64": None,
            "role": "unknown",
        },
        {
            "device_id": "node_candidate_2",
            "ipv6_address": "fd35:5807:223f:1:235e:586d:bd3b:4921",
            "eui64": None,
            "role": "unknown",
        },
        {
            "device_id": "node_candidate_3",
            "ipv6_address": "fdde:ad00:beef:0:235e:586d:bd3b:4921",
            "eui64": None,
            "role": "unknown",
        },
    ]


def test_otbr_candidates_are_probed_before_interface_and_multicast():
    class FakeRegistry:
        def __init__(self):
            self.calls = []

        async def register_device(self, ipv6_addr, eui64=None, resources=None):
            self.calls.append((ipv6_addr, eui64, resources))
            return {"device_id": "thread_otbr", "ipv6_addr": ipv6_addr}

    async def scenario():
        registry = FakeRegistry()
        discovery = CoAPDiscovery(
            registry,
            {"otbr_rest_url": "http://172.30.32.1:8081"},
        )
        discovery.context = object()
        discovery.start_cycle()

        async def fake_resolve_base_url():
            return "http://172.30.32.1:8081"

        async def fake_fetch_otbr_candidates(base_url):
            assert base_url == "http://172.30.32.1:8081"
            return [
                {
                    "device_id": "de62e016db392476",
                    "ipv6_address": "fd35:5807:223f:1:235e:586d:bd3b:4921",
                    "eui64": "62:34:56:78:90:aa:cd:ea",
                    "role": "child",
                }
            ]

        async def fake_query(ipv6_addr, timeout=65.0):
            assert timeout == 65.0
            assert ipv6_addr == "fd35:5807:223f:1:235e:586d:bd3b:4921"
            return [{"uri_path": "/auth", "resource_type": "auth", "observable": False}]

        discovery._resolve_otbr_base_url = fake_resolve_base_url
        discovery._fetch_otbr_candidates = fake_fetch_otbr_candidates
        discovery.query_device_resources = fake_query

        results = await discovery.discover_otbr_devices()

        assert registry.calls == [
            (
                "fd35:5807:223f:1:235e:586d:bd3b:4921",
                "62:34:56:78:90:aa:cd:ea",
                [{"uri_path": "/auth", "resource_type": "auth", "observable": False}],
            )
        ]
        assert results == [{"device_id": "thread_otbr", "ipv6_addr": "fd35:5807:223f:1:235e:586d:bd3b:4921"}]

    run(scenario())
