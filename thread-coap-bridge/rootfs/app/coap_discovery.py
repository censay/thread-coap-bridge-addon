"""
CoAP Discovery Module

Handles seed-based unicast bootstrap, multicast discovery,
and parsing of /.well-known/core responses.
"""

import asyncio
import ipaddress
import json
import logging
import re
from asyncio.subprocess import PIPE
from urllib import error as urllib_error
from urllib import request as urllib_request
from aiocoap import Context, Message, GET, NON

logger = logging.getLogger(__name__)


class CoAPDiscovery:
    """CoAP device discovery via multicast."""

    WELL_KNOWN_CORE = "/.well-known/core"
    DISCOVERY_TIMEOUT = 5.0
    COMMAND_TIMEOUT = 3.0
    OTBR_HTTP_TIMEOUT = 5.0
    OTBR_ACTION_POLL_INTERVAL = 1.0
    OTBR_ACTION_MAX_WAIT = 20.0
    MULTICAST_GROUPS = ['ff03::fd', 'ff03::1']

    def __init__(self, device_registry, config):
        self.registry = device_registry
        self.multicast_address = config.get('multicast_address', 'ff03::fd')
        self.thread_interface = config.get('thread_interface', 'wpan0')
        self.otbr_rest_urls = self._build_otbr_rest_urls(config.get('otbr_rest_url'))
        self.seed_addresses = self._normalize_seed_addresses(
            config.get('seed_ipv6_addresses', [])
        )
        self.context = None
        self.cycle_addresses = set()

        logger.info(f"CoAP Discovery initialized (trying groups: {', '.join(self.MULTICAST_GROUPS)})")
        if self.otbr_rest_urls:
            logger.info(
                "OTBR REST discovery enabled via %s",
                ", ".join(self.otbr_rest_urls),
            )
        if self.seed_addresses:
            logger.info(
                "Configured %d seed IPv6 address(es) for unicast bootstrap",
                len(self.seed_addresses),
            )

    async def initialize(self):
        try:
            self.context = await Context.create_client_context()
            logger.info("CoAP context initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize CoAP context: {e}")
            raise

    def start_cycle(self):
        self.cycle_addresses = set()

    async def discover_seed_devices(self):
        if not self.context:
            logger.error("CoAP context not initialized")
            return []

        results = []

        for ipv6_addr in self.seed_addresses:
            if ipv6_addr in self.cycle_addresses:
                continue

            logger.info("Probing seed device via unicast: %s", ipv6_addr)
            resources = await self.query_device_resources(ipv6_addr)
            if not resources:
                logger.debug("Seed device did not respond: %s", ipv6_addr)
                continue

            self.cycle_addresses.add(ipv6_addr)
            logger.info("Seed device responded: %s", ipv6_addr)
            results.append(await self.registry.register_device(ipv6_addr, resources=resources))

        return results

    async def discover_devices(self):
        """Perform one discovery cycle and reconcile all replies."""
        if not self.context:
            logger.error("CoAP context not initialized")
            return []

        results = []
        results.extend(await self.discover_otbr_devices())
        results.extend(await self.discover_interface_devices())

        for mcast_addr in self.MULTICAST_GROUPS:
            try:
                uri = f'coap://[{mcast_addr}%{self.thread_interface}]{self.WELL_KNOWN_CORE}'
                logger.info(f"Sending multicast discovery to {uri}")

                request = Message(code=GET, uri=uri, mtype=NON)
                pending = self.context.request(request)

                try:
                    response = await asyncio.wait_for(
                        pending.response,
                        timeout=self.DISCOVERY_TIMEOUT,
                    )

                    if response and response.payload:
                        source_addr = self._extract_source_address(response)
                        if source_addr and source_addr not in self.cycle_addresses:
                            payload_str = response.payload.decode('utf-8').rstrip('\x00')
                            logger.info(f"✓ Discovered device at {source_addr} via {mcast_addr}")
                            logger.info(f"Device resources: {payload_str}")

                            self.cycle_addresses.add(source_addr)
                            resources = self._parse_core_link_format(payload_str)
                            if resources:
                                results.append(await self.registry.register_device(source_addr, resources=resources))

                except asyncio.TimeoutError:
                    logger.debug(f"No response from {mcast_addr} (timeout)")

            except Exception as e:
                logger.error(f"Error with multicast {mcast_addr}: {e}")

        if not self.cycle_addresses:
            logger.warning("No devices discovered via multicast - devices may not be responding to multicast")

        return results

    async def discover_otbr_devices(self):
        """Use OTBR REST inventory as the primary source of attached Thread devices."""
        if not self.context:
            logger.error("CoAP context not initialized")
            return []

        if not self.otbr_rest_urls:
            return []

        candidates = []
        reachable_urls = []

        for base_url in self.otbr_rest_urls:
            devices = await self._fetch_otbr_devices(base_url)
            if devices is None:
                continue

            reachable_urls.append(base_url)
            candidates.extend(self._extract_otbr_candidates(devices))

        if not reachable_urls:
            logger.warning(
                "OTBR REST not reachable via %s",
                ", ".join(self.otbr_rest_urls),
            )
            return []

        if not candidates:
            logger.info(
                "No OTBR REST device candidates available from %s",
                ", ".join(reachable_urls),
            )
            return []

        logger.info("OTBR REST returned %d candidate(s)", len(candidates))

        results = []
        for candidate in candidates:
            ipv6_addr = candidate['ipv6_address']
            if ipv6_addr in self.cycle_addresses:
                continue

            logger.info(
                "Probing OTBR device candidate: %s (%s)",
                ipv6_addr,
                candidate['device_id'],
            )
            resources = await self.query_device_resources(ipv6_addr)
            if not resources:
                logger.debug("OTBR candidate did not respond: %s", ipv6_addr)
                continue

            self.cycle_addresses.add(ipv6_addr)
            logger.info("OTBR device responded: %s", ipv6_addr)
            results.append(
                await self.registry.register_device(
                    ipv6_addr,
                    eui64=candidate.get('eui64'),
                    resources=resources,
                )
            )

        return results

    async def discover_interface_devices(self):
        """Probe IPv6 candidates derived from the local Thread interface state."""
        if not self.context:
            logger.error("CoAP context not initialized")
            return []

        candidates = await self._collect_interface_candidates()
        if not candidates:
            logger.info(
                "No interface-derived IPv6 candidates found on %s",
                self.thread_interface,
            )
            return []

        logger.info(
            "Discovered %d interface-derived candidate(s) on %s",
            len(candidates),
            self.thread_interface,
        )

        results = []
        for ipv6_addr in candidates:
            if ipv6_addr in self.cycle_addresses:
                continue

            logger.info("Probing interface-derived candidate: %s", ipv6_addr)
            resources = await self.query_device_resources(ipv6_addr)
            if not resources:
                logger.debug("Candidate did not respond: %s", ipv6_addr)
                continue

            self.cycle_addresses.add(ipv6_addr)
            logger.info("Interface-derived device responded: %s", ipv6_addr)
            results.append(
                await self.registry.register_device(ipv6_addr, resources=resources)
            )

        return results

    async def query_device_resources(self, ipv6_addr, timeout=65.0):
        logger.debug(f"Querying resources from {ipv6_addr}")

        if not self.context:
            logger.error("CoAP context not initialized")
            return []

        try:
            uri = f'coap://[{ipv6_addr}]{self.WELL_KNOWN_CORE}'
            request = Message(code=GET, uri=uri)
            response = await asyncio.wait_for(
                self.context.request(request).response,
                timeout=timeout,
            )

            if response.payload:
                payload = response.payload.decode('utf-8').rstrip('\x00')
                logger.info(f"Received resources from {ipv6_addr}: {payload}")
                return self._parse_core_link_format(payload)

            logger.warning(f"Empty response from {ipv6_addr}")
            return []

        except asyncio.TimeoutError:
            logger.warning(f"Query timeout for device {ipv6_addr}")
            return []
        except Exception as e:
            logger.error(f"Error querying device {ipv6_addr}: {e}")
            return []

    def _parse_core_link_format(self, payload):
        resources = []

        try:
            for match in re.finditer(r'<([^>]+)>([^,]*)', payload):
                uri_path = match.group(1)
                attributes = match.group(2) or ''
                rt_match = re.search(r';rt="([^"]+)"', attributes)
                iface_match = re.search(r';if="([^"]+)"', attributes)
                resource = {
                    'uri_path': uri_path,
                    'resource_type': rt_match.group(1) if rt_match else 'unknown',
                    'interface_type': iface_match.group(1) if iface_match else None,
                    'observable': ';obs' in attributes,
                }
                resources.append(resource)
                logger.debug(f"Parsed resource: {resource}")

            logger.info(f"Parsed {len(resources)} resources from CoRE Link Format")

        except Exception as e:
            logger.error(f"Error parsing CoRE Link Format: {e}")

        return resources

    def _extract_source_address(self, response):
        try:
            if hasattr(response, 'remote') and response.remote:
                if isinstance(response.remote, tuple):
                    addr = response.remote[0]
                else:
                    addr = str(response.remote)

                if '<UDP6EndpointAddress' in addr:
                    match = re.search(r'\[([0-9a-f:]+)\]', addr)
                    if match:
                        addr = match.group(1)

                addr = addr.strip('[]')
                addr = addr.split('%')[0]
                logger.debug(f"Extracted address: {addr}")
                return addr
            return None
        except Exception as e:
            logger.error(f"Error extracting source address: {e}")
            return None

    def forget_device(self, ipv6_addr):
        """Compatibility hook; discovery now rechecks devices every cycle."""
        if ipv6_addr in self.cycle_addresses:
            self.cycle_addresses.discard(ipv6_addr)
            logger.debug(f"Removed {ipv6_addr} from current discovery cycle cache")

    async def rediscover_offline_devices(self, registry):
        """Attempt unicast rediscovery of known offline devices."""
        try:
            devices = await registry.get_offline_devices()
            if not devices:
                return []

            logger.debug(f"Attempting unicast rediscovery of {len(devices)} offline device(s)")
            results = []

            for device in devices:
                ipv6 = device.ipv6_address
                if ipv6 in self.cycle_addresses:
                    continue

                logger.debug(f"Probing {device.device_id} at {ipv6}")
                resources = await self.query_device_resources(ipv6)
                if resources:
                    logger.info(f"Re-discovered offline device {device.device_id} at {ipv6}")
                    self.cycle_addresses.add(ipv6)
                    results.append(await self.registry.register_device(ipv6, resources=resources))

            return results

        except Exception as e:
            logger.error(f"Unicast rediscovery error: {e}")
            return []

    async def shutdown(self):
        if self.context:
            await self.context.shutdown()
            logger.info("CoAP discovery context shut down")

    async def _fetch_otbr_devices(self, base_url):
        action_id = await self._trigger_otbr_device_refresh(base_url)
        if action_id:
            await self._wait_for_otbr_action(base_url, action_id)

        response = await self._otbr_request_json(
            'GET',
            f'{base_url}/devices',
            headers={'Accept': 'application/vnd.api+json'},
        )
        if not response:
            return None

        data = response.get('data')
        if isinstance(data, list):
            return data

        if isinstance(response, list):
            return response

        return []

    async def _trigger_otbr_device_refresh(self, base_url):
        payload = {
            'data': [
                {
                    'type': 'updateDeviceCollectionTask',
                    'attributes': {
                        'maxAge': 30,
                        'maxRetries': 3,
                        'deviceCount': 50,
                        'timeout': 15,
                    },
                }
            ]
        }

        response = await self._otbr_request_json(
            'POST',
            f'{base_url}/actions',
            headers={
                'Accept': 'application/vnd.api+json',
                'Content-Type': 'application/vnd.api+json',
            },
            payload=payload,
        )
        if not response:
            return None

        data = response.get('data')
        if isinstance(data, list) and data:
            action = data[0]
        elif isinstance(data, dict):
            action = data
        else:
            logger.debug("Unexpected OTBR action response: %s", response)
            return None

        action_id = action.get('id')
        if action_id:
            logger.debug("Started OTBR device refresh action %s", action_id)
        return action_id

    async def _wait_for_otbr_action(self, base_url, action_id):
        deadline = asyncio.get_running_loop().time() + self.OTBR_ACTION_MAX_WAIT

        while asyncio.get_running_loop().time() < deadline:
            response = await self._otbr_request_json(
                'GET',
                f'{base_url}/actions/{action_id}',
                headers={'Accept': 'application/vnd.api+json'},
            )
            if not response:
                return

            data = response.get('data')
            if isinstance(data, list):
                data = data[0] if data else {}

            status = ((data or {}).get('attributes') or {}).get('status')
            if status == 'completed':
                logger.debug("OTBR device refresh action %s completed", action_id)
                return

            if status in {'failed', 'stopped'}:
                logger.warning("OTBR device refresh action %s ended with status=%s", action_id, status)
                return

            await asyncio.sleep(self.OTBR_ACTION_POLL_INTERVAL)

        logger.warning("Timed out waiting for OTBR action %s", action_id)

    async def _otbr_request_json(self, method, url, headers=None, payload=None):
        return await asyncio.to_thread(
            self._otbr_request_json_sync,
            method,
            url,
            headers or {},
            payload,
        )

    def _otbr_request_json_sync(self, method, url, headers, payload):
        data = None
        request_headers = dict(headers)
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            request_headers.setdefault('Content-Type', 'application/json')

        request = urllib_request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method,
        )

        try:
            with urllib_request.urlopen(request, timeout=self.OTBR_HTTP_TIMEOUT) as response:
                body = response.read().decode('utf-8')
        except urllib_error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            logger.debug("OTBR HTTP %s for %s: %s", exc.code, url, body)
            return None
        except urllib_error.URLError as exc:
            logger.debug("OTBR request failed for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.debug("Unexpected OTBR request failure for %s: %s", url, exc)
            return None

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.debug("Invalid OTBR JSON from %s: %s", url, body)
            return None

    def _extract_otbr_candidates(self, devices):
        candidates = []
        seen = set()

        for device in devices:
            device_type = device.get('type')
            if device_type and device_type != 'threadDevice':
                continue

            attributes = device.get('attributes') or device
            device_id = device.get('id') or attributes.get('extAddress')
            if not device_id:
                continue

            omr_addresses = attributes.get('omrIpv6Address')
            if isinstance(omr_addresses, str):
                omr_addresses = [omr_addresses]
            elif not isinstance(omr_addresses, list):
                omr_addresses = []

            eui64 = attributes.get('eui64') or attributes.get('eui')

            for address in omr_addresses:
                normalized = self._normalize_candidate(address)
                if not normalized or normalized in seen:
                    continue

                seen.add(normalized)
                candidates.append(
                    {
                        'device_id': device_id,
                        'ipv6_address': normalized,
                        'eui64': eui64,
                        'role': attributes.get('role'),
                    }
                )

        return candidates

    async def _collect_interface_candidates(self):
        candidates = set()

        local_addrs_output = await self._run_command(
            ['ip', '-6', 'addr', 'show', 'dev', self.thread_interface]
        )
        local_addrs = self._extract_interface_addresses(local_addrs_output)

        neigh_output = await self._run_command(
            ['ip', '-6', 'neigh', 'show', 'dev', self.thread_interface]
        )
        candidates.update(self._extract_neighbor_candidates(neigh_output))

        route_output = await self._run_command(
            ['ip', '-6', 'route', 'show', 'dev', self.thread_interface]
        )
        candidates.update(self._extract_route_candidates(route_output))

        filtered = sorted(addr for addr in candidates if addr not in local_addrs)
        logger.debug(
            "Interface candidates after filtering local addresses: %s",
            filtered,
        )
        return filtered

    async def _run_command(self, argv):
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=PIPE,
                stderr=PIPE,
            )
        except FileNotFoundError:
            logger.warning("Command not available: %s", argv[0])
            return ""
        except Exception as exc:
            logger.error("Failed to start command %s: %s", argv, exc)
            return ""

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.COMMAND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            logger.warning("Command timed out: %s", " ".join(argv))
            return ""

        if process.returncode != 0:
            err_text = stderr.decode('utf-8', errors='ignore').strip()
            logger.debug(
                "Command returned %d for %s: %s",
                process.returncode,
                " ".join(argv),
                err_text,
            )
            return ""

        return stdout.decode('utf-8', errors='ignore')

    def _extract_interface_addresses(self, output):
        candidates = set()

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if 'inet6 ' not in line:
                continue

            parts = line.split()
            try:
                inet6_idx = parts.index('inet6')
            except ValueError:
                continue

            if len(parts) <= inet6_idx + 1:
                continue

            token = parts[inet6_idx + 1].split('/')[0]
            normalized = self._normalize_candidate(token)
            if normalized:
                candidates.add(normalized)

        return candidates

    def _extract_neighbor_candidates(self, output):
        candidates = set()

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            token = line.split()[0]
            normalized = self._normalize_candidate(token)
            if normalized:
                candidates.add(normalized)

        return candidates

    def _extract_route_candidates(self, output):
        candidates = set()

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            token = line.split()[0]
            if token == 'default':
                continue

            if '/' in token:
                try:
                    network = ipaddress.IPv6Network(token, strict=False)
                except ValueError:
                    continue

                if network.prefixlen != 128:
                    continue

                token = str(network.network_address)

            normalized = self._normalize_candidate(token)
            if normalized:
                candidates.add(normalized)

        return candidates

    def _normalize_candidate(self, value):
        candidate = str(value).strip()
        if not candidate:
            return None

        candidate = candidate.strip('[]')
        candidate = candidate.split('%')[0]

        try:
            addr = ipaddress.IPv6Address(candidate)
        except ValueError:
            return None

        if (
            addr.is_multicast
            or addr.is_link_local
            or addr.is_loopback
            or addr.is_unspecified
        ):
            return None

        return addr.compressed

    def _build_otbr_rest_urls(self, value):
        defaults = [
            value,
            'http://127.0.0.1:8081/api',
            'http://localhost:8081/api',
            'http://core-openthread-border-router:8081/api',
        ]

        urls = []
        seen = set()

        for candidate in defaults:
            normalized = self._normalize_otbr_rest_url(candidate)
            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            urls.append(normalized)

        return urls

    def _normalize_otbr_rest_url(self, value):
        if value is None:
            return None

        candidate = str(value).strip()
        if not candidate:
            return None

        return candidate.rstrip('/')

    def _normalize_seed_addresses(self, values):
        if values is None:
            return []

        if isinstance(values, str):
            values = [values]

        normalized = []
        seen = set()

        for value in values:
            if value is None:
                continue

            candidate = str(value).strip()
            if not candidate:
                continue

            candidate = candidate.strip('[]')
            candidate = candidate.split('%')[0]

            try:
                ipaddress.IPv6Address(candidate)
            except ValueError:
                logger.warning("Ignoring invalid seed IPv6 address: %s", value)
                continue

            if candidate in seen:
                continue

            seen.add(candidate)
            normalized.append(candidate)

        return normalized
