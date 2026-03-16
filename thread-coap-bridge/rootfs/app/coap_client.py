"""
CoAP Client Module

Handles GET/PUT/POST operations and CoAP Observe for real-time updates.
"""

import asyncio
import inspect
import json
import logging
from aiocoap import Context, Message, GET, PUT, POST, NON

logger = logging.getLogger(__name__)

READ_TIMEOUT = 15.0
WRITE_TIMEOUT = 20.0
OBSERVE_REGISTRATION_TIMEOUT = 20.0
MAX_RETRY_DELAY = 60.0


class CoAPClient:
    """CoAP client for device interaction."""

    def __init__(self, mqtt_publisher):
        self.mqtt = mqtt_publisher
        self.context = None
        self.observations = {}
        self.reobserve_tasks = set()
        self.running = True
        self.device_status_callback = None

        logger.info("CoAP Client initialized")

    def set_status_callback(self, callback):
        self.device_status_callback = callback

    def _track_reobserve_task(self, task):
        self.reobserve_tasks.add(task)
        task.add_done_callback(self.reobserve_tasks.discard)
        return task

    def _cancel_observation(self, obs_key, observation_request=None):
        entry = self.observations.get(obs_key)
        if observation_request is None and entry:
            observation_request = entry.get("request")

        if observation_request is not None:
            try:
                observation_request.observation.cancel()
            except Exception:
                pass

        current = self.observations.get(obs_key)
        if current and (observation_request is None or current.get("request") is observation_request):
            del self.observations[obs_key]

    async def initialize(self):
        try:
            self.context = await Context.create_client_context()
            logger.info("CoAP client context initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize CoAP context: {e}")
            raise

    async def _notify_device_status(self, device_id, is_online):
        if not self.device_status_callback:
            return

        try:
            result = self.device_status_callback(device_id, is_online)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.error("Device status callback failed for %s: %s", device_id, exc)

    async def get_resource(self, ipv6_addr, uri_path, timeout=READ_TIMEOUT):
        logger.debug(f"GET coap://[{ipv6_addr}]{uri_path}")

        if not self.context:
            logger.error("CoAP context not initialized")
            return None

        try:
            uri = f'coap://[{ipv6_addr}]{uri_path}'
            request = Message(code=GET, uri=uri, mtype=NON)

            response = await asyncio.wait_for(
                self.context.request(request).response,
                timeout=timeout,
            )

            if response.code.is_successful():
                payload = response.payload.decode('utf-8').rstrip('\x00')
                logger.debug(f"GET response from {ipv6_addr}{uri_path}: {payload}")
                return payload

            logger.warning(f"GET failed with code {response.code}")
            return None

        except asyncio.TimeoutError:
            logger.warning(f"GET timeout for {ipv6_addr}{uri_path}")
            return None
        except Exception as e:
            logger.error(f"GET error for {ipv6_addr}{uri_path}: {e}")
            return None

    async def put_resource(self, ipv6_addr, uri_path, payload, timeout=WRITE_TIMEOUT):
        return await self._send_with_payload(PUT, ipv6_addr, uri_path, payload, timeout=timeout)

    async def post_resource(self, ipv6_addr, uri_path, payload, timeout=WRITE_TIMEOUT):
        return await self._send_with_payload(POST, ipv6_addr, uri_path, payload, timeout=timeout)

    async def _send_with_payload(self, method, ipv6_addr, uri_path, payload, timeout=WRITE_TIMEOUT):
        logger.debug(f"{method} coap://[{ipv6_addr}]{uri_path} = {payload}")

        if not self.context:
            logger.error("CoAP context not initialized")
            return None

        try:
            uri = f'coap://[{ipv6_addr}]{uri_path}'

            if isinstance(payload, str):
                payload_bytes = payload.encode('utf-8')
            elif isinstance(payload, dict):
                payload_bytes = json.dumps(payload).encode('utf-8')
            else:
                payload_bytes = payload

            request = Message(code=method, uri=uri, payload=payload_bytes)
            response = await asyncio.wait_for(
                self.context.request(request).response,
                timeout=timeout,
            )

            if response.code.is_successful():
                body = response.payload.decode('utf-8').rstrip('\x00') if response.payload else ''
                logger.info(f"{method} successful for {ipv6_addr}{uri_path}")
                return body or True

            logger.warning(f"{method} failed with code {response.code}")
            return None

        except asyncio.TimeoutError:
            logger.warning(f"{method} timeout for {ipv6_addr}{uri_path}")
            return None
        except Exception as e:
            logger.error(f"{method} error for {ipv6_addr}{uri_path}: {e}")
            return None

    async def observe_resource(self, device_id, ipv6_addr, uri_path,
                               registry=None, offline_threshold=5, discovery=None):
        logger.info(f"Starting observation: {device_id} - coap://[{ipv6_addr}]{uri_path}")

        if not self.context:
            logger.error("CoAP context not initialized")
            return

        obs_key = f"{device_id}{uri_path}"
        consecutive_failures = 0
        device_is_online = True
        max_reconnect_attempts = 10

        while self.running and consecutive_failures < max_reconnect_attempts:
            retry_delay = min(MAX_RETRY_DELAY, 10.0 * max(1, consecutive_failures))
            observation_request = None
            try:
                uri = f'coap://[{ipv6_addr}]{uri_path}'
                request = Message(code=GET, uri=uri, observe=0, mtype=NON)
                observation_request = self.context.request(request)

                self.observations[obs_key] = {
                    'request': observation_request,
                    'device_id': device_id,
                    'ipv6_addr': ipv6_addr,
                    'uri_path': uri_path,
                    'registry': registry,
                    'offline_threshold': offline_threshold,
                    'discovery': discovery,
                }

                try:
                    initial_response = await asyncio.wait_for(
                        observation_request.response,
                        timeout=OBSERVE_REGISTRATION_TIMEOUT,
                    )

                    if initial_response.code.is_successful():
                        consecutive_failures = 0

                        if registry:
                            await registry.update_device_failure(device_id, failed=False)

                        if not device_is_online:
                            logger.info(f"Device {device_id} back online (observe)")
                            self.mqtt.publish_availability(device_id, available=True)
                            await self._notify_device_status(device_id, True)
                            device_is_online = True

                        payload = initial_response.payload.decode('utf-8').rstrip('\x00')
                        logger.info(f"Observe established for {device_id}{uri_path}")
                        try:
                            state_value = json.loads(payload)
                        except json.JSONDecodeError:
                            state_value = payload
                        self.mqtt.publish_state(device_id, uri_path, state_value)

                    else:
                        logger.warning(f"Observe registration failed: {initial_response.code}")
                        consecutive_failures += 1
                        self._cancel_observation(obs_key, observation_request)
                        await asyncio.sleep(min(MAX_RETRY_DELAY, 10.0 * consecutive_failures))
                        continue

                except asyncio.TimeoutError:
                    logger.warning(f"Observe registration timeout for {device_id}{uri_path}")
                    consecutive_failures += 1
                    # Treat initial observe registration as resource-local
                    # failure, not proof that the whole device is gone. Polls
                    # or a later announce may still succeed while this observe
                    # stream is being re-established.
                    self._cancel_observation(obs_key, observation_request)
                    await asyncio.sleep(min(MAX_RETRY_DELAY, 10.0 * consecutive_failures))
                    continue

                try:
                    async for response in observation_request.observation:
                        if not self.running:
                            break

                        if response.code.is_successful():
                            payload = response.payload.decode('utf-8').rstrip('\x00')
                            logger.info(f"Observe notification from {device_id}{uri_path}: {payload}")

                            consecutive_failures = 0
                            if registry:
                                await registry.update_device_failure(device_id, failed=False)

                            try:
                                state_value = json.loads(payload)
                            except json.JSONDecodeError:
                                state_value = payload

                            self.mqtt.publish_state(device_id, uri_path, state_value)
                        else:
                            logger.warning(f"Observe notification error: {response.code}")
                except Exception as obs_error:
                    logger.warning(f"Observation iteration error for {device_id}{uri_path}: {obs_error}")

                self._cancel_observation(obs_key, observation_request)
                logger.info(f"Observe stream ended for {device_id}{uri_path}, waiting before retry...")
                await asyncio.sleep(max(30.0, retry_delay))

            except asyncio.CancelledError:
                self._cancel_observation(obs_key, observation_request)
                logger.info(f"Observation cancelled for {device_id}{uri_path}")
                break
            except Exception as e:
                logger.error(f"Observe error for {device_id}{uri_path}: {e}")
                consecutive_failures += 1
                self._cancel_observation(obs_key, observation_request)
                await asyncio.sleep(min(MAX_RETRY_DELAY, 10.0 * consecutive_failures))

        if obs_key in self.observations:
            del self.observations[obs_key]

        if consecutive_failures >= max_reconnect_attempts:
            logger.warning(f"Giving up observe for {device_id}{uri_path} after {consecutive_failures} failures")
            if discovery:
                discovery.forget_device(ipv6_addr)

    async def reregister_observers(self, device_id):
        logger.info(f"Re-registering observers for {device_id} after reboot detected")

        device_observations = [
            (key, obs) for key, obs in self.observations.items()
            if obs.get('device_id') == device_id
        ]

        for obs_key, obs_info in device_observations:
            try:
                if 'request' in obs_info:
                    try:
                        obs_info['request'].observation.cancel()
                    except Exception:
                        pass

                del self.observations[obs_key]

                self._track_reobserve_task(asyncio.create_task(
                    self.observe_resource(
                        obs_info['device_id'],
                        obs_info['ipv6_addr'],
                        obs_info['uri_path'],
                        registry=obs_info.get('registry'),
                        offline_threshold=obs_info.get('offline_threshold', 5),
                        discovery=obs_info.get('discovery'),
                    ),
                    name=f"reobserve_{device_id}_{obs_info['uri_path']}",
                ))
                logger.info(f"Re-started observation for {device_id}{obs_info['uri_path']}")

            except Exception as e:
                logger.error(f"Error re-registering observer {obs_key}: {e}")

    async def shutdown(self):
        logger.info("Shutting down CoAP client")

        self.running = False

        for obs_key, observation in list(self.observations.items()):
            try:
                observation['request'].observation.cancel()
                logger.debug(f"Cancelled observation: {obs_key}")
            except Exception as e:
                logger.error(f"Error cancelling observation {obs_key}: {e}")

        self.observations.clear()

        for task in list(self.reobserve_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.reobserve_tasks.clear()

        if self.context:
            await self.context.shutdown()
            logger.info("CoAP client context shut down")
