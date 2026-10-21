"""Deployment inputs must move together and preserve the auth boundaries."""
import os
import subprocess
import sys

import pytest

from hkparking.auth.runtime import RuntimeConfig
from hkparking.deployment import DeploymentSettings


@pytest.mark.parametrize('name,value', [
    ('CARPARK_PUBLIC_ORIGIN','http://parking.example.test'),
    ('CARPARK_PUBLIC_ORIGIN','https://parking.example.test/path'),
    ('CARPARK_PUBLIC_ORIGIN','https://PARKING.example.test'),
    ('CARPARK_CLIENT_REDIRECT_URI','https://*.example.test/callback'),
    ('CARPARK_CLIENT_REDIRECT_URI','https://user:password@example.test/callback'),
    ('CARPARK_CLIENT_REDIRECT_URI','https://example.test/callback#fragment'),
    ('CARPARK_CLIENT_REDIRECT_URI','http://example.test/callback'),
    ('CARPARK_DISPLAY_NAME',''),
    ('CARPARK_SHORT_NAME','line\nbreak'),
    ('CARPARK_ICON_TEXT','too long'),
])
def test_reject_unsafe_environment_choices(name,value):
    with pytest.raises(ValueError):
        DeploymentSettings.from_env({name:value})


def runtime_env(tmp_path):
    database=tmp_path/'identity.db'
    database.touch()
    secret=tmp_path/'secret'
    secret.write_text('SYNTHETIC-secret\n')
    secret.chmod(0o600)
    st=database.stat()
    return {
        'CARPARK_PUBLIC_ORIGIN':'https://parking.example.test',
        'CARPARK_POCKET_CLIENT_ID':'SYNTHETIC-client',
        'CARPARK_ALLOWED_SUB':'SYNTHETIC-owner',
        'CARPARK_STATE_DIR':str(tmp_path/'state'),
        'CARPARK_POCKET_DATABASE':str(database),
        'CARPARK_POCKET_CLIENT_SECRET_FILE':str(secret),
        'CARPARK_POCKET_DB_DEVICE':str(st.st_dev),
        'CARPARK_POCKET_DB_INODE':str(st.st_ino),
    }


def test_runtime_uses_private_secret_file_and_bound_database(tmp_path):
    env=runtime_env(tmp_path)
    cfg=RuntimeConfig.from_env(env)
    assert cfg.read_secret()=='SYNTHETIC-secret'
    assert cfg.state_dir==tmp_path/'state'
    assert 'SYNTHETIC-secret' not in repr(cfg)
    cfg.secret_file.chmod(0o644)
    with pytest.raises(ValueError,match='private'):
        cfg.read_secret()
    cfg.secret_file.chmod(0o600)
    env['CARPARK_POCKET_DB_INODE']=str(int(env['CARPARK_POCKET_DB_INODE'])+1)
    with pytest.raises(ValueError,match='binding'):
        RuntimeConfig.from_env(env)


@pytest.mark.parametrize('name,value', [
    ('CARPARK_STATE_DIR','relative/state'), ('CARPARK_POCKET_PORT','65536'),
    ('CARPARK_LISTEN_PORT','0'), ('CARPARK_LISTEN_PORT','bad'),
    ('CARPARK_POCKET_DB_DEVICE',''), ('CARPARK_PUBLIC_ORIGIN',''),
])
def test_runtime_rejects_missing_or_invalid_values(tmp_path,name,value):
    env=runtime_env(tmp_path)
    env[name]=value
    with pytest.raises(ValueError):
        RuntimeConfig.from_env(env)


def test_runtime_additional_subjects_are_explicit(tmp_path):
    env = runtime_env(tmp_path)
    assert RuntimeConfig.from_env(env).additional_allowed_subs == ()
    env['CARPARK_ADDITIONAL_ALLOWED_SUBS'] = '["SYNTHETIC-friend"]'
    assert RuntimeConfig.from_env(env).additional_allowed_subs == ('SYNTHETIC-friend',)
    for invalid in ['null', '{}', '"any"', '[""]', '[1]', '["client-machine"]', 'not-json']:
        env['CARPARK_ADDITIONAL_ALLOWED_SUBS'] = invalid
        with pytest.raises(ValueError):
            RuntimeConfig.from_env(env)


def test_alternate_environment_preserves_refresh_and_header_policy(tmp_path):
    # Fresh process: deployment settings are intentionally fixed at startup.
    env={key:value for key,value in os.environ.items() if not key.startswith('CARPARK_')}
    env.update(CARPARK_PUBLIC_ORIGIN='https://parking.example.test:9443',
               CARPARK_CLIENT_REDIRECT_URI='https://assistant.example.test/callback',
               CARPARK_DISPLAY_NAME='Example & Parking',CARPARK_SHORT_NAME='Park')
    code=r'''
import asyncio
from pathlib import Path
import httpx
from hkparking.auth import integration as m
from hkparking.auth.bridge import CALLBACK
from hkparking.auth.consent import render_consent
from tests.test_refresh_lifetime import test_native_lifetime_survives_rotation_and_restart, pocket_fixture
root=Path(__import__('sys').argv[1])
assert m.PUBLIC=='https://parking.example.test:9443'
assert CALLBACK=='https://assistant.example.test/callback'
root.joinpath('refresh').mkdir()
test_native_lifetime_survives_rotation_and_restart(root/'refresh')
root.joinpath('headers').mkdir()
app,verifier,*_=pocket_fixture(root/'headers')
async def check():
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=m.PUBLIC) as http:
        r=await http.get('/.well-known/oauth-authorization-server')
        assert r.status_code==200 and r.json()['authorization_endpoint']==m.PUBLIC+'/bridge/authorize'
        assert (await http.get('/.well-known/oauth-authorization-server',headers={'Host':'carpark.example.test'})).status_code==421
        assert (await http.get('/.well-known/oauth-authorization-server',headers={'Origin':'null'})).status_code==403
    await verifier.aclose()
asyncio.run(check())
page=render_consent(client_name='Assistant',client_id='SYNTHETIC-client',redirect_uri=CALLBACK,scopes=['parking:read'],txn_id='SYNTHETIC',csrf_token='SYNTHETIC')
assert 'to Example &amp; Parking?</h1>' in page
assert '<span>Park</span>' in page
assert CALLBACK in page
print('alternate configuration: refresh rotation/restart/replay, metadata, Host/Origin and branding passed')
'''
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],env=env,capture_output=True,text=True,check=False)
    assert result.returncode==0,result.stdout+result.stderr
