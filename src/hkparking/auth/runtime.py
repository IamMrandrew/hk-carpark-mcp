"""Environment-only production startup; never logs secrets or callback queries."""
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from hkparking.deployment import DeploymentSettings


def required(env,name):
    value=env.get(name)
    if not value:
        raise ValueError(f'{name} is required')
    return value


def number(env,name,default=None,minimum=0,maximum=None):
    raw=env.get(name,default)
    try:
        result=int(raw)
    except (ValueError,TypeError):
        raise ValueError(f'{name} must be an integer') from None
    if result<minimum or maximum is not None and result>maximum:
        raise ValueError(f'{name} is out of range')
    return result


def absolute_path(env,name):
    p=Path(required(env,name))
    if not p.is_absolute() or p.resolve()!=p:
        raise ValueError(f'{name} must be an absolute canonical path')
    return p


@dataclass(frozen=True)
class RuntimeConfig:
    state_dir: Path
    identity_db: Path
    secret_file: Path
    client_id: str
    allowed_sub: str
    pocket_port: int
    listen_port: int
    identity_file: tuple[int,int]
    additional_allowed_subs: tuple[str, ...] = ()

    @classmethod
    def from_env(cls,env=None):
        env=os.environ if env is None else env
        required(env,'CARPARK_PUBLIC_ORIGIN')
        DeploymentSettings.from_env(env)
        additional = json.loads(env.get('CARPARK_ADDITIONAL_ALLOWED_SUBS', '[]'))
        if type(additional) is not list or any(
            not isinstance(sub, str) or not sub or sub.startswith('client-') for sub in additional
        ):
            raise ValueError('CARPARK_ADDITIONAL_ALLOWED_SUBS must be a JSON array of user subjects')
        cfg=cls(
            state_dir=absolute_path(env,'CARPARK_STATE_DIR'),
            identity_db=absolute_path(env,'CARPARK_POCKET_DATABASE'),
            secret_file=absolute_path(env,'CARPARK_POCKET_CLIENT_SECRET_FILE'),
            client_id=required(env,'CARPARK_POCKET_CLIENT_ID'),
            allowed_sub=required(env,'CARPARK_ALLOWED_SUB'),
            additional_allowed_subs=tuple(additional),
            pocket_port=number(env,'CARPARK_POCKET_PORT','14110',1,65535),
            listen_port=number(env,'CARPARK_LISTEN_PORT','8767',1,65535),
            identity_file=(number(env,'CARPARK_POCKET_DB_DEVICE'),number(env,'CARPARK_POCKET_DB_INODE')))
        metadata=cfg.identity_db.stat()
        if (metadata.st_dev,metadata.st_ino)!=cfg.identity_file:
            raise ValueError('Pocket database does not match its approved device/inode binding')
        return cfg

    def read_secret(self):
        if self.secret_file.is_symlink():
            raise ValueError('Client secret file must not be a symlink')
        metadata=self.secret_file.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size>4096:
            raise ValueError('Client secret file must be private (0600 or 0400) and at most 4096 bytes')
        value=self.secret_file.read_text().strip()
        if not value:
            raise ValueError('Client secret file is empty')
        return value


def main():
    import uvicorn

    from .integration import PocketConfig, build_integration
    from .native_status import RELEASE

    cfg=RuntimeConfig.from_env()
    pocket=PocketConfig(cfg.allowed_sub,cfg.client_id,cfg.pocket_port,str(cfg.identity_db),RELEASE,cfg.identity_file,
                        additional_allowed_subs=cfg.additional_allowed_subs)
    app=build_integration(cfg.state_dir,pocket,cfg.read_secret())
    uvicorn.run(app,host='127.0.0.1',port=cfg.listen_port,access_log=False,proxy_headers=False,log_level='info')
