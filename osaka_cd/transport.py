"""Publication endpoint policy shared by VPS setup and the Termux client.

HTTP is permitted only for a literal Tailscale address in an explicitly paired
VPN configuration. The address range alone does not prove the VPN is active.
"""
from __future__ import annotations

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit, urlunsplit

PUBLISH_PATH = '/api/osaka/cd/publish'
TAILSCALE_NETWORKS = (ip_network('100.64.0.0/10'), ip_network('fd7a:115c:a1e0::/48'))


class TransportError(ValueError):
    """Stable local error codes, without endpoint credentials."""


def is_tailscale_address(host):
    if not isinstance(host, str) or '%' in host:
        return False
    try:
        address = ip_address(host)
    except (ValueError, TypeError):
        return False
    return any(address.version == network.version and address in network for network in TAILSCALE_NETWORKS)


def _endpoint(url, transport):
    if not isinstance(transport, str) or transport not in {'https', 'tailscale'}:
        raise TransportError('publish_transport_invalid')
    if (not isinstance(url, str) or not url or '\\' in url or '?' in url or '#' in url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
        raise TransportError('publish_url_invalid')
    try:
        endpoint = urlsplit(url)
        # Accessing these properties also rejects invalid brackets and ports.
        host, port = endpoint.hostname, endpoint.port
    except ValueError:
        raise TransportError('publish_url_invalid') from None
    if (not host or endpoint.username is not None or endpoint.password is not None
            or endpoint.query or endpoint.fragment or (port is not None and not 1 <= port <= 65535)
            or endpoint.netloc.endswith(':')):
        raise TransportError('publish_url_invalid')
    if endpoint.scheme == 'https':
        return endpoint
    if endpoint.scheme != 'http' or transport != 'tailscale':
        raise TransportError('publish_url_requires_https_or_explicit_tailscale')
    if not is_tailscale_address(host):
        raise TransportError('publish_http_requires_literal_tailscale_address')
    return endpoint


def validate_publish_url(url, transport='https'):
    endpoint = _endpoint(url, transport)
    if endpoint.path not in {PUBLISH_PATH, PUBLISH_PATH + '/'}:
        raise TransportError('publish_url_invalid_path')
    return urlunsplit((endpoint.scheme, endpoint.netloc, PUBLISH_PATH, '', ''))


def normalize_publish_url(url, transport='https'):
    """Allow a service base URL during setup; refuse unrelated paths."""
    endpoint = _endpoint(url, transport)
    if endpoint.path in {'', '/'}:
        url = urlunsplit((endpoint.scheme, endpoint.netloc, PUBLISH_PATH, '', ''))
    return validate_publish_url(url, transport)


def validate_status_endpoint(url, transport='https'):
    """Validate network destination before constructing an authenticated request."""
    endpoint = _endpoint(url, transport)
    if not endpoint.path.startswith('/api/osaka/cd/'):
        raise TransportError('publish_status_url_refused')
    return url
