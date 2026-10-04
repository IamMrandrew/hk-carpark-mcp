"""Explicit public OAuth resource and a private loopback listener."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl


@dataclass(frozen=True, slots=True)
class ServerConfig:
    issuer_url: str
    public_origin: str = "https://carpark.laporatory.com"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8767

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.public_origin)
            valid = (
                isinstance(self.public_origin, str)
                and not any(c.isspace() for c in self.public_origin)
                and url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.path
                and not url.query
                and not url.fragment
            )
            # Accessing port also rejects malformed/out-of-range ports.
            _ = url.port
            # Reject, rather than silently rewrite, origins normalized differently
            # by the SDK. Our exact Host/Origin guard must use the same spelling.
            valid = valid and self.public_origin == str(
                AnyHttpUrl(self.public_origin)
            ).removesuffix("/")
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("public_origin must be an HTTPS origin without credentials or a path")

        try:
            issuer = urlsplit(self.issuer_url)
            issuer_valid = (
                isinstance(self.issuer_url, str)
                and not any(c.isspace() for c in self.issuer_url)
                and issuer.scheme == "https"
                and bool(issuer.hostname)
                and issuer.username is None
                and issuer.password is None
                and not issuer.query
                and not issuer.fragment
            )
            _ = issuer.port
        except (TypeError, ValueError, AttributeError):
            issuer_valid = False
        if not issuer_valid:
            raise ValueError("issuer_url must be an explicit HTTPS issuer without credentials")
        if self.listen_host != "127.0.0.1":
            raise ValueError("listen_host must be 127.0.0.1; use the tunnel for public access")
        if type(self.listen_port) is not int or not 1 <= self.listen_port <= 65535:
            raise ValueError("listen_port must be an integer in 1..65535")

    @property
    def resource_url(self) -> str:
        return self.public_origin + "/mcp"
