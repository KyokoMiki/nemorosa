"""SCGI XMLRPC Transport.

XMLRPC in Python only supports HTTP(S). This module extends the transport to also support SCGI.

SCGI is required by rTorrent if you want to communicate directly with an instance.
"""

import socket
from io import BytesIO
from urllib.parse import urlparse
from xmlrpc.client import Transport  # nosec B411

import defusedxml.xmlrpc

# Monkey-patch xmlrpc.client to mitigate XML vulnerabilities
defusedxml.xmlrpc.monkey_patch()


def encode_netstring(input_data: bytes) -> bytes:
    """Encode data as netstring format."""
    return str(len(input_data)).encode() + b":" + input_data + b","


def encode_header(key: bytes, value: bytes) -> bytes:
    """Encode SCGI header."""
    return key + b"\x00" + value + b"\x00"


class SCGITransport(Transport):
    """SCGI transport for XML-RPC."""

    def __init__(self, *args, **kwargs):
        self.socket_path = kwargs.pop("socket_path", "")
        Transport.__init__(self, *args, **kwargs)

    def single_request(self, host, handler, request_body, verbose=False):
        """Make a single SCGI request."""
        self.verbose = verbose
        address = None

        if self.socket_path:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address = self.socket_path
        else:
            # host is a string in format "hostname:port"
            host_str = str(host) if not isinstance(host, str) else host
            # Add dummy scheme for urlparse (only for parsing, not part of SCGI protocol)
            parsed = urlparse(f"scgi://{host_str}")
            if not parsed.hostname or not parsed.port:
                raise ValueError(f"Invalid host format '{host}', expected 'hostname:port'")
            s = socket.socket(socket.AF_INET6 if ":" in parsed.hostname else socket.AF_INET, socket.SOCK_STREAM)
            address = (parsed.hostname, parsed.port)

        try:
            s.settimeout(30)
            s.connect(address)

            request = encode_header(b"CONTENT_LENGTH", str(len(request_body)).encode())
            request += encode_header(b"SCGI", b"1")
            request += encode_header(b"REQUEST_METHOD", b"POST")
            request += encode_header(b"REQUEST_URI", handler.encode())

            request = encode_netstring(request)
            request += request_body

            s.sendall(request)
            s.shutdown(socket.SHUT_WR)  # Signal no more data will be sent

            response = b""
            while True:
                r = s.recv(1024)
                if not r:
                    break
                response += r

            response_body = BytesIO(b"\r\n\r\n".join(response.split(b"\r\n\r\n")[1:]))

            return self.parse_response(response_body)  # type: ignore[arg-type]
        finally:
            s.close()


if not hasattr(Transport, "single_request"):
    SCGITransport.request = SCGITransport.single_request
