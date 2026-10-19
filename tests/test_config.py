"""Production configuration tests; no credentials or real identity provider."""

import importlib.util

import pytest


def test_production_configuration_keeps_public_resource_and_loopback_binding():
    assert importlib.util.find_spec("hkparking.config"), "Production configuration is not implemented"
    from hkparking.config import ServerConfig

    config = ServerConfig(issuer_url="https://auth.example.test")
    assert config.public_origin == "https://carpark.example.test"
    assert config.resource_url == "https://carpark.example.test/mcp"
    assert config.listen_host == "127.0.0.1"
    assert config.listen_port == 8767


@pytest.mark.parametrize(
    "origin",
    [
        "http://carpark.example.test",
        "https://carpark.example.test/extra",
        "https://carpark.example.test/",
        "https://carpark.example.test?q=1",
        "https://carpark.example.test#fragment",
        "https://fixture-user:fixture-password@example.test",
        "https://carpark.example.test:bad",
        "https://carpark.example.test\n",
    ],
)
def test_rejects_invalid_public_origins(origin):
    from hkparking.config import ServerConfig

    with pytest.raises(ValueError, match="public_origin"):
        ServerConfig(issuer_url="https://auth.example.test", public_origin=origin)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer_url", ""),
        ("issuer_url", "http://auth.example.test"),
        ("issuer_url", "https://fixture-user:fixture-password@auth.example.test"),
        ("issuer_url", "https://auth.example.test?key=fixture"),
        ("issuer_url", "https://auth.example.test#fragment"),
        ("issuer_url", "https://auth.example.test:bad"),
        ("listen_host", "0.0.0.0"),
        ("listen_host", "192.0.2.1"),
        ("listen_port", 0),
        ("listen_port", 65536),
        ("listen_port", True),
        ("listen_port", "8767"),
    ],
)
def test_rejects_unsafe_listener_or_missing_secure_issuer(field, value):
    from hkparking.config import ServerConfig

    options = {"issuer_url": "https://auth.example.test", field: value}
    with pytest.raises(ValueError, match=field):
        ServerConfig(**options)


@pytest.mark.parametrize(
    "origin",
    [
        "https://CARPARK.example.test",
        "https://carpark.example.test:443",
        "https://停車場.example.test",
    ],
)
def test_rejects_noncanonical_origins_before_exact_header_checks(origin):
    from hkparking.config import ServerConfig

    with pytest.raises(ValueError, match="public_origin"):
        ServerConfig(issuer_url="https://auth.example.test", public_origin=origin)
