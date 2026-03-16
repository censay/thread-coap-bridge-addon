import asyncio
import hashlib
import os
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

from main import CoAPBridgeService


def run(coro):
    return asyncio.run(coro)


def test_verify_auth_signature_accepts_raw_r_s_encoding():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_hex = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    ).hex()
    nonce = os.urandom(32)
    digest = hashlib.sha256(nonce).digest()
    der_signature = private_key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    r, s = decode_dss_signature(der_signature)
    raw_signature_hex = (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()

    service = CoAPBridgeService()

    assert service._verify_auth_signature(public_key_hex, nonce, raw_signature_hex) is True
    assert service._verify_auth_signature(public_key_hex, nonce, "00" * 64) is False


def test_finished_resource_tasks_do_not_requeue_after_clean_exit():
    async def scenario():
        service = CoAPBridgeService()

        async def completed_poll():
            return None

        task = asyncio.create_task(completed_poll(), name="poll_thread_dev_/uptime")
        await task

        service.resource_tasks[("thread_dev", "/uptime")] = {
            "task": task,
            "spec": object(),
        }

        service._prune_finished_tasks()

        assert "thread_dev" not in service.reconcile_requested
        assert service.resource_tasks == {}

    run(scenario())


def test_crashed_resource_tasks_are_requeued_for_reconcile():
    async def scenario():
        service = CoAPBridgeService()

        async def crashed_poll():
            raise RuntimeError("boom")

        task = asyncio.create_task(crashed_poll(), name="poll_thread_dev_/uptime")
        try:
            await task
        except RuntimeError:
            pass

        service.resource_tasks[("thread_dev", "/uptime")] = {
            "task": task,
            "spec": object(),
        }

        service._prune_finished_tasks()

        assert "thread_dev" in service.reconcile_requested
        assert service.resource_tasks == {}

    run(scenario())


def test_duplicate_announce_within_window_is_ignored():
    async def scenario():
        service = CoAPBridgeService()
        seen = []

        class FakeDiscovery:
            async def process_announcement(self, ipv6_address, eui64=None):
                seen.append((ipv6_address, eui64))
                return SimpleNamespace(device_id="thread_dev")

        service.discovery = FakeDiscovery()

        await service._handle_device_announce("fd00::1234", "abcd", {})
        await service._handle_device_announce("fd00::1234", "abcd", {})

        assert seen == [("fd00::1234", "abcd")]
        assert service.reconcile_requested == {"thread_dev"}

    run(scenario())
