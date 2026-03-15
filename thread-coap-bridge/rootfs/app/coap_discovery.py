"""
CoAP Discovery Module

Handles OTBR-assisted discovery, seed-based unicast bootstrap,
multicast discovery, and parsing of /.well-known/core responses.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from aiocoap import Context, Message, GET, NON

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str
    json_data: object | None


class CoAPDiscovery:
    """CoAP device discovery via multicast."""

    WELL_KNOWN_CORE = "/.well-known/core"
    DISCOVERY_TIMEOUT = 5.0
    COMMAND_TIMEOUT = 3.0
    HTTP_TIMEOUT = 5.0
    SUPERVISOR_URL = "http://supervisor"
    OTBR_INVENTORY_PATH = "/api/devices"
    OTBR_NODE_PATH = "/node"
    OTBR_DIAGNOSTIC_PATHS = ("/", "/get_properties", "/api/node", "/node")
    OTBR_ADDON_SLUG_CANDIDATES = (
        "core_openthread_border_router",
        "local_openthread_border_router",
    )
    MULTICAST_GROUPS = ['ff03::fd', 'ff03::1']

    def __init__(self, device_registry, config):
        self.registry = device_registry
        self.multicast_address = config.get('multicast_address', 'ff03::fd')
        self.thread_interface = config.get('thread_interface', 'wpan0')
        self.otbr_base_url_override = self._normalize_otbr_base_url(
            config.get('otbr_rest_url')
        )
        self.seed_addresses = self._normalize_seed_addresses(
            config.get('seed_ipv6_addresses', [])
        )
        self.supervisor_token = os.getenv('SUPERVISOR_TOKEN')
        self.context = None
        self.cycle_addresses = set()
        self.otbr_base_url = None
        self.otbr_inventory_supported = None
        self._logged_events = set()

        logger.info(f"CoAP Discovery initialized (trying groups: {', '.join(self.MULTICAST_GROUPS)})")
        if self.otbr_base_url_override:
            logger.info("OTBR web override configured: %s", self.otbr_base_url_override)
        elif self.supervisor_token:
            logger.info("OTBR discovery will resolve the border router via Supervisor API")
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
        """Use OTBR discovery surfaces exposed by the installed HA OTBR build."""
        if not self.context:
            logger.error("CoAP context not initialized")
            return []

        base_url = await self._resolve_otbr_base_url()
        if not base_url:
            return []

        candidates = await self._fetch_otbr_candidates(base_url)
        if not candidates:
            return []

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

    async def _fetch_otbr_candidates(self, base_url):
        if self.otbr_inventory_supported is False:
            return await self._fetch_otbr_node_candidates(base_url)

        response = await self._http_request(
            'GET',
            f'{base_url}{self.OTBR_INVENTORY_PATH}',
            headers={'Accept': 'application/json, application/vnd.api+json'},
        )
        if response is None:
            self._log_once(
                f'otbr-unreachable:{base_url}',
                logging.WARNING,
                "OTBR web not reachable at %s",
                base_url,
            )
            return None

        if response.status == 404:
            self.otbr_inventory_supported = False
            logger.warning(
                "OTBR web is reachable at %s but %s returned HTTP 404; switching to %s fallback",
                base_url,
                self.OTBR_INVENTORY_PATH,
                self.OTBR_NODE_PATH,
            )
            await self._probe_otbr_surface(base_url)
            return await self._fetch_otbr_node_candidates(base_url)

        if response.status >= 400:
            logger.warning(
                "OTBR inventory request returned HTTP %s from %s%s",
                response.status,
                base_url,
                self.OTBR_INVENTORY_PATH,
            )
            return None

        self.otbr_inventory_supported = True
        payload = self._unwrap_data(response.json_data)
        if isinstance(payload, list):
            candidates = self._extract_otbr_candidates(payload)
        elif isinstance(payload, dict):
            if isinstance(payload.get('devices'), list):
                candidates = self._extract_otbr_candidates(payload['devices'])
            elif isinstance(payload.get('items'), list):
                candidates = self._extract_otbr_candidates(payload['items'])
            else:
                logger.debug(
                    "OTBR inventory payload shape was not recognized from %s%s: %s",
                    base_url,
                    self.OTBR_INVENTORY_PATH,
                    response.body,
                )
                return []
        else:
            logger.debug(
                "OTBR inventory payload shape was not recognized from %s%s: %s",
                base_url,
                self.OTBR_INVENTORY_PATH,
                response.body,
            )
            return []

        logger.info("OTBR inventory returned %d candidate(s)", len(candidates))
        if not candidates:
            logger.info(
                "No OTBR inventory device candidates available from %s",
                base_url,
            )
        return candidates

    async def _fetch_otbr_node_candidates(self, base_url):
        response = await self._http_request(
            'GET',
            f'{base_url}{self.OTBR_NODE_PATH}',
            headers={'Accept': 'application/json, text/plain, */*'},
        )
        if response is None:
            logger.warning("OTBR node request failed for %s%s", base_url, self.OTBR_NODE_PATH)
            return []

        if response.status >= 400:
            logger.warning(
                "OTBR node request returned HTTP %s from %s%s",
                response.status,
                base_url,
                self.OTBR_NODE_PATH,
            )
            return []

        self._log_otbr_node_payload(base_url, response)
        local_addrs = await self._collect_local_interface_addresses()
        candidates = self._extract_otbr_node_candidates(
            response.json_data,
            local_addrs=local_addrs,
        )
        logger.info(
            "OTBR %s fallback produced %d candidate(s) after filtering local addresses",
            self.OTBR_NODE_PATH,
            len(candidates),
        )
        if not candidates:
            logger.info(
                "No OTBR %s candidates were derivable from %s",
                self.OTBR_NODE_PATH,
                base_url,
            )
        return candidates

    async def _resolve_otbr_base_url(self):
        if self.otbr_base_url_override:
            return self.otbr_base_url_override

        if self.otbr_base_url:
            return self.otbr_base_url

        if not self.supervisor_token:
            self._log_once(
                'otbr-supervisor-token-missing',
                logging.INFO,
                "Supervisor API token not available; OTBR auto-resolution is disabled",
            )
            return None

        addon_slug = await self._resolve_otbr_addon_slug()
        if not addon_slug:
            self._log_once(
                'otbr-addon-missing',
                logging.WARNING,
                "OpenThread Border Router add-on was not found via Supervisor API",
            )
            return None

        response = await self._supervisor_request(
            'GET',
            f'/addons/{urllib_parse.quote(addon_slug, safe="")}/info',
        )
        if response is None:
            self._log_once(
                f'otbr-addon-info-unreachable:{addon_slug}',
                logging.WARNING,
                "Supervisor API could not load OTBR add-on info for %s",
                addon_slug,
            )
            return None

        if response.status >= 400:
            self._log_once(
                f'otbr-addon-info-http:{addon_slug}:{response.status}',
                logging.WARNING,
                "Supervisor API returned HTTP %s for OTBR add-on info (%s)",
                response.status,
                addon_slug,
            )
            return None

        info = self._unwrap_data(response.json_data)
        base_url = self._extract_otbr_base_url(info)
        if not base_url:
            self._log_once(
                f'otbr-addon-info-missing-url:{addon_slug}',
                logging.WARNING,
                "Supervisor API returned OTBR add-on info without a reachable web bind for %s",
                addon_slug,
            )
            return None

        self.otbr_base_url = base_url
        logger.info("Resolved OTBR web base via Supervisor API: %s", base_url)
        return base_url

    async def _resolve_otbr_addon_slug(self):
        for slug in self.OTBR_ADDON_SLUG_CANDIDATES:
            response = await self._supervisor_request(
                'GET',
                f'/addons/{urllib_parse.quote(slug, safe="")}/info',
            )
            if response is None:
                continue

            if response.status == 200:
                return slug

            if response.status == 404:
                continue

            if response.status == 403:
                self._log_once(
                    f'otbr-addon-info-http:{slug}:403',
                    logging.WARNING,
                    "Supervisor API returned HTTP 403 for OTBR add-on info (%s)",
                    slug,
                )
                return None

        return await self._find_otbr_addon_slug()

    async def _find_otbr_addon_slug(self):
        response = await self._supervisor_request('GET', '/addons')
        if response is None:
            self._log_once(
                'supervisor-addons-unreachable',
                logging.WARNING,
                "Supervisor API could not list installed add-ons",
            )
            return None

        if response.status >= 400:
            self._log_once(
                f'supervisor-addons-http:{response.status}',
                logging.WARNING,
                "Supervisor API returned HTTP %s while listing add-ons",
                response.status,
            )
            return None

        data = self._unwrap_data(response.json_data)
        if isinstance(data, dict):
            addons = data.get('addons', [])
        elif isinstance(data, list):
            addons = data
        else:
            addons = []

        preferred = []
        fallback = []
        for addon in addons:
            slug = addon.get('slug')
            name = (addon.get('name') or '').lower()
            if not slug:
                continue
            if slug == 'core_openthread_border_router':
                preferred.append(slug)
            elif slug.endswith('openthread_border_router') or name == 'openthread border router':
                fallback.append(slug)

        candidates = preferred or fallback
        return candidates[0] if candidates else None

    async def _probe_otbr_surface(self, base_url):
        statuses = []
        for path in self.OTBR_DIAGNOSTIC_PATHS:
            response = await self._http_request(
                'GET',
                f'{base_url}{path}',
                headers={'Accept': 'application/json, text/plain, */*'},
            )
            if response is None:
                statuses.append(f'{path}=unreachable')
            else:
                statuses.append(f'{path}={response.status}')

        logger.info(
            "OTBR web surface at %s responded with %s",
            base_url,
            ", ".join(statuses),
        )

    def _log_otbr_node_payload(self, base_url, response):
        payload = response.json_data
        preview = (response.body or "").replace("\n", " ").strip()
        if len(preview) > 300:
            preview = preview[:300] + "..."

        if isinstance(payload, dict):
            logger.info(
                "OTBR %s payload from %s has top-level keys: %s",
                self.OTBR_NODE_PATH,
                base_url,
                ", ".join(sorted(payload.keys())) or "(none)",
            )
        elif isinstance(payload, list):
            logger.info(
                "OTBR %s payload from %s is a list with %d item(s)",
                self.OTBR_NODE_PATH,
                base_url,
                len(payload),
            )
        else:
            logger.info(
                "OTBR %s payload from %s is non-JSON or unstructured",
                self.OTBR_NODE_PATH,
                base_url,
            )

        if preview:
            logger.info("OTBR %s preview: %s", self.OTBR_NODE_PATH, preview)

    async def _supervisor_request(self, method, path, payload=None):
        return await self._http_request(
            method,
            f'{self.SUPERVISOR_URL}{path}',
            headers={
                'Authorization': f'Bearer {self.supervisor_token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            payload=payload,
        )

    async def _http_request(self, method, url, headers=None, payload=None):
        return await asyncio.to_thread(
            self._http_request_sync,
            method,
            url,
            headers or {},
            payload,
        )

    def _http_request_sync(self, method, url, headers, payload):
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
            with urllib_request.urlopen(request, timeout=self.HTTP_TIMEOUT) as response:
                body = response.read().decode('utf-8', errors='ignore')
                status = response.getcode()
        except urllib_error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            return HttpResponse(exc.code, body, self._decode_json_body(body))
        except urllib_error.URLError as exc:
            logger.debug("HTTP request failed for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.debug("Unexpected HTTP request failure for %s: %s", url, exc)
            return None

        return HttpResponse(status, body, self._decode_json_body(body))

    def _decode_json_body(self, body):
        if not body:
            return None

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _unwrap_data(self, payload):
        if isinstance(payload, dict) and 'data' in payload:
            return payload['data']
        return payload

    def _extract_otbr_base_url(self, info):
        if not isinstance(info, dict):
            return None

        ip_address = info.get('ip_address')
        if ip_address:
            return self._normalize_otbr_base_url(f'http://{ip_address}:8081')

        hostname = info.get('hostname')
        if hostname:
            return self._normalize_otbr_base_url(f'http://{hostname}:8081')

        return None

    def _log_once(self, key, level, message, *args):
        if key in self._logged_events:
            return

        self._logged_events.add(key)
        logger.log(level, message, *args)

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

    def _extract_otbr_node_candidates(self, payload, local_addrs=None):
        local_addrs = local_addrs or set()
        addresses = sorted(self._collect_ipv6_values(payload))
        candidates = []
        next_index = 1

        for address in addresses:
            if address in local_addrs:
                continue

            candidates.append(
                {
                    'device_id': f'node_candidate_{next_index}',
                    'ipv6_address': address,
                    'eui64': None,
                    'role': 'unknown',
                }
            )
            next_index += 1

        return candidates

    def _collect_ipv6_values(self, payload):
        found = set()

        def visit(value):
            if isinstance(value, dict):
                for nested in value.values():
                    visit(nested)
                return

            if isinstance(value, list):
                for nested in value:
                    visit(nested)
                return

            if isinstance(value, str):
                normalized = self._normalize_candidate(value)
                if normalized:
                    found.add(normalized)

        visit(payload)
        return found

    async def _collect_local_interface_addresses(self):
        output = await self._run_command(
            ['ip', '-6', 'addr', 'show', 'dev', self.thread_interface]
        )
        return self._extract_interface_addresses(output)

    async def _collect_interface_candidates(self):
        candidates = set()

        local_addrs = await self._collect_local_interface_addresses()

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
        candidate = candidate.split('/')[0]
        candidate = candidate.rstrip(',;')

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

    def _normalize_otbr_base_url(self, value):
        if value is None:
            return None

        candidate = str(value).strip()
        if not candidate:
            return None

        candidate = candidate.rstrip('/')
        if candidate.endswith('/api'):
            candidate = candidate[:-4]

        return candidate

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
