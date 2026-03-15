"""
Incoming CoAP announce server.

Devices can POST to /announce after Thread attach. The bridge uses the
source IPv6 plus the announced device_id to register the device and then
continues with normal /.well-known/core-based reconciliation.
"""

import inspect
import json
import logging

from aiocoap import CHANGED, Context, Message, resource

logger = logging.getLogger(__name__)


class _AnnounceResource(resource.Resource):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    async def render_post(self, request):
        source_ip = self._extract_source_ipv6(request)
        if not source_ip:
            logger.warning("Ignoring announce without source IPv6")
            return Message(code=CHANGED, payload=b"")

        payload = self._decode_payload(request.payload)
        device_id = payload.get("device_id")
        if isinstance(device_id, str):
            device_id = device_id.strip().lower() or None
        else:
            device_id = None

        logger.info("Received CoAP announce from %s (device_id=%s)", source_ip, device_id)

        result = self._callback(source_ip, device_id, payload)
        if inspect.isawaitable(result):
            await result

        return Message(code=CHANGED, payload=b"")

    @staticmethod
    def _decode_payload(payload_bytes):
        if not payload_bytes:
            return {}

        try:
            payload_text = payload_bytes.decode("utf-8").rstrip("\x00")
            return json.loads(payload_text)
        except Exception as exc:
            logger.warning("Failed to parse announce payload: %s", exc)
            return {}

    @staticmethod
    def _extract_source_ipv6(request):
        remote = getattr(request, "remote", None)
        if not remote:
            return None

        sockaddr = getattr(remote, "sockaddr", None)
        if isinstance(sockaddr, tuple) and sockaddr:
            return str(sockaddr[0]).split("%", 1)[0]

        hostinfo = getattr(remote, "hostinfo", "")
        if hostinfo.startswith("["):
            end = hostinfo.find("]")
            if end > 1:
                return hostinfo[1:end]

        return None


class CoAPAnnounceServer:
    ANNOUNCE_URI = ["announce"]
    ANNOUNCE_GROUP = "ff03::1"
    ANNOUNCE_PORT = 5685

    def __init__(self, config, callback):
        self.thread_interface = config.get("thread_interface", "wpan0")
        self.callback = callback
        self.context = None

    async def initialize(self):
        site = resource.Site()
        site.add_resource(self.ANNOUNCE_URI, _AnnounceResource(self.callback))
        multicast = [(self.ANNOUNCE_GROUP, self.thread_interface)]
        self.context = await Context.create_server_context(
            site,
            bind=("::", self.ANNOUNCE_PORT),
            multicast=multicast,
        )
        logger.info(
            "CoAP announce server listening on /announce via %s%%%s port %d",
            self.ANNOUNCE_GROUP,
            self.thread_interface,
            self.ANNOUNCE_PORT,
        )

    async def shutdown(self):
        if self.context:
            await self.context.shutdown()
            logger.info("CoAP announce server shut down")
