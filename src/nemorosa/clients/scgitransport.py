"""Async SCGI XMLRPC Transport.

XMLRPC in Python only supports HTTP(S). This module extends the transport
to also support SCGI. SCGI is required by rTorrent if you want to communicate
directly with an instance.
"""

import xmlrpc.client  # nosec B411
from io import BytesIO
from urllib.parse import urlparse

import anyio
import defusedxml.xmlrpc
from aioxmlrpc.client import ServerProxy

from .client_common import TORRENT_CLIENT_TIMEOUT


def encode_netstring(input_data: bytes) -> bytes:
    """Encode data as netstring format."""
    return str(len(input_data)).encode() + b":" + input_data + b","


def encode_header(key: bytes, value: bytes) -> bytes:
    """Encode SCGI header."""
    return key + b"\x00" + value + b"\x00"


class AsyncSCGITransport(xmlrpc.client.Transport):
    """Async SCGI transport for XML-RPC, compatible with aioxmlrpc."""

    def __init__(self, *, socket_path: str = "") -> None:
        # Monkey-patch xmlrpc.client to mitigate XML vulnerabilities
        defusedxml.xmlrpc.monkey_patch()

        self.socket_path = socket_path
        super().__init__()

    async def request(  # type: ignore[override]
        self,
        host: str,
        handler: str,
        request_body: bytes,
        verbose: bool = False,
    ) -> tuple:
        """Send an SCGI request and return the parsed response.

        Args:
            host: Host in "hostname:port" format (ignored when using a
                Unix socket).
            handler: Request URI (e.g. "/RPC2").
            request_body: Marshalled XML-RPC request body.
            verbose: Unused, kept for interface compatibility.

        Returns:
            The parsed XML-RPC response.
        """
        self.verbose = verbose

        request = encode_header(b"CONTENT_LENGTH", str(len(request_body)).encode())
        request += encode_header(b"SCGI", b"1")
        request += encode_header(b"REQUEST_METHOD", b"POST")
        request += encode_header(b"REQUEST_URI", handler.encode())

        request = encode_netstring(request)
        request += request_body

        with anyio.fail_after(TORRENT_CLIENT_TIMEOUT):
            if self.socket_path:
                stream = await anyio.connect_unix(self.socket_path)
            else:
                # host is a string in format "hostname:port"
                # Add dummy scheme for urlparse (not part of SCGI protocol)
                parsed = urlparse(f"scgi://{host}")
                if not parsed.hostname or not parsed.port:
                    raise ValueError(
                        f"Invalid host format '{host}', expected 'hostname:port'"
                    )

                stream = await anyio.connect_tcp(parsed.hostname, parsed.port)

            async with stream:
                await stream.send(request)
                await stream.send_eof()  # Signal no more data will be sent

                response = b""
                while True:
                    try:
                        response += await stream.receive(1024)
                    except anyio.EndOfStream:
                        break

        # Split only once at first blank line to separate headers from body
        parts = response.split(b"\r\n\r\n", 1)
        response_body = BytesIO(parts[1] if len(parts) > 1 else b"")

        return self.parse_response(response_body)  # type: ignore[arg-type]


class SCGIServerProxy(ServerProxy):
    """aioxmlrpc-compatible ServerProxy that talks SCGI instead of HTTP."""

    def __init__(self, uri: str, *, socket_path: str = "") -> None:
        # Bypass aioxmlrpc's __init__ (which builds an httpx transport) and
        # initialize the base ServerProxy with the async SCGI transport.
        xmlrpc.client.ServerProxy.__init__(  # pylint: disable=non-parent-init-called
            self,
            uri,
            transport=AsyncSCGITransport(socket_path=socket_path),
        )
