"""Synthetic HTTPS client test of the temporary parking preview, no public deployment."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx


def test_temporary_preview_requires_auth_and_truthful_parking_consent(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "parking_preview.py"
    assert path.is_file(), "Runnable parking preview not implemented"
    spec = importlib.util.spec_from_file_location("parking_preview_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app, provider = module.build_app("https://synthetic-parking.example", tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://synthetic-parking.example"
            ) as http:
                denied = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert denied.status_code == 401
                metadata = await http.get("/.well-known/oauth-authorization-server")
                assert metadata.status_code == 200
                assert metadata.json()["issuer"] == "https://synthetic-parking.example/"
                registration = await http.post(
                    "/register",
                    json={
                        "client_name": "Synthetic test client",
                        "redirect_uris": ["https://synthetic-client.invalid/callback"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert registration.status_code == 201
                client_id = registration.json()["client_id"]
                request = await http.get(
                    "/authorize",
                    params={
                        "client_id": client_id,
                        "response_type": "code",
                        "redirect_uri": "https://synthetic-client.invalid/callback",
                        "scope": "parking:read",
                        "state": "synthetic-state",
                        "code_challenge": "SYNTHETIC_CHALLENGE_abcdefghijklmnopqrstuvwxyz",
                        "code_challenge_method": "S256",
                    },
                )
                assert request.status_code == 302
                page = await http.get(request.headers["location"])
                assert page.status_code == 200
                assert "read-only government parking" in page.text
                assert "only probe_status" not in page.text
                assert len(provider.pending) == 1
                assert not provider.codes and not provider.access, "Waiting page must not approve grants"
                assert (await http.post("/approve")).status_code == 404
        finally:
            await app.state.parking_http.aclose()

    asyncio.run(run())


def test_preview_runs_from_a_standalone_copy_without_parent_workspace(tmp_path):
    """Synthetic fixture: a release must not depend on a private sibling spike."""
    import shutil

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    copied = tmp_path / "standalone" / "scripts"
    shutil.copytree(scripts, copied)
    path = copied / "parking_preview.py"
    spec = importlib.util.spec_from_file_location("standalone_parking_preview_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app, provider = module.build_app("https://synthetic-parking.example", tmp_path / "runtime")
    try:
        assert not provider.access and not provider.refresh
        assert module.AUTH_MODULE.is_relative_to(copied)
    finally:
        asyncio.run(app.state.parking_http.aclose())
