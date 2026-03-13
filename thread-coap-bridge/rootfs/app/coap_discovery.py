"""
CoAP Discovery Module

Handles seed-based unicast bootstrap, multicast discovery,
and parsing of /.well-known/core responses.
"""

import asyncio
import ipaddress
import logging
import re
from asyncio.subprocess import PIPE
from aiocoap import Context, Message, GET, NON

logger = logging.getLogger(__name__)


class CoAPDiscovery:
    """CoAP device discovery via multicast."""

    WELL_KNOWN_CORE = "/.well-known/core"
    DISCOVERY_TIMEOUT = 5.0
    COMMAND_TIMEOUT = 3.0
    MULTICAST_GROUPS = ['ff03::fd', 'ff03::1']

    def __init__(self, device_registry, config):
        self.registry = device_registry
        self.multicast_address = config.get('multicast_address', 'ff03::fd')
        self.thread_interface = config.get('thread_interface', 'wpan0')
        self.seed_addresses = self._normalize_seed_addresses(
            config.get('seed_ipv6_addresses', [])
        )
        self.context = None
        self.cycle_addresses = set()

        logger.info(f"CoAP Discovery initialized (trying groups: {', '.join(self.MULTICAST_GROUPS)})")
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
