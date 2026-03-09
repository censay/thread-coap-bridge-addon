from coap_discovery import CoAPDiscovery


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
