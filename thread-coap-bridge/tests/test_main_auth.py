import asyncio
import hashlib
import os

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


def test_finished_resource_tasks_are_requeued_for_reconcile():
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

        assert "thread_dev" in service.reconcile_requested

    run(scenario())
