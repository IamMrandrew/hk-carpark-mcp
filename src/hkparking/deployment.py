"""Validated deployment choices, read once when the process starts."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl


def secure_url(value, name, *, origin=False):
    try:
        u=urlsplit(value)
        valid=(isinstance(value,str) and not any(c.isspace() for c in value)
               and u.scheme=='https' and bool(u.hostname) and u.username is None
               and u.password is None and not u.fragment and '*' not in value)
        _=u.port
        if origin:
            valid=valid and not u.path and not u.query
        canonical=str(AnyHttpUrl(value))
        if origin:
            canonical=canonical.removesuffix('/')
        valid=valid and canonical==value
    except (ValueError,TypeError,AttributeError):
        valid=False
    if not valid:
        raise ValueError(f'{name} must be a canonical HTTPS '+('origin' if origin else 'URL'))
    return value


def label(value,name):
    if not isinstance(value,str) or not value.strip() or len(value)>80 or any(ord(c)<32 for c in value):
        raise ValueError(f'{name} must be a nonempty label of at most 80 characters')
    return value


@dataclass(frozen=True)
class DeploymentSettings:
    public_origin: str = 'https://carpark.example.test'
    client_redirect_uri: str = 'https://grok.com/connectors-oauth-exchange-code/'
    display_name: str = 'Carpark'
    short_name: str = 'Carpark'
    icon_text: str = 'P'

    def __post_init__(self):
        secure_url(self.public_origin,'CARPARK_PUBLIC_ORIGIN',origin=True)
        secure_url(self.client_redirect_uri,'CARPARK_CLIENT_REDIRECT_URI')
        label(self.display_name,'CARPARK_DISPLAY_NAME')
        label(self.short_name,'CARPARK_SHORT_NAME')
        if len(label(self.icon_text,'CARPARK_ICON_TEXT')) != 1:
            raise ValueError('CARPARK_ICON_TEXT must be one character')

    @classmethod
    def from_env(cls,env=None):
        env=os.environ if env is None else env
        return cls(**{field:env[key] for field,key in {
            'public_origin':'CARPARK_PUBLIC_ORIGIN',
            'client_redirect_uri':'CARPARK_CLIENT_REDIRECT_URI',
            'display_name':'CARPARK_DISPLAY_NAME',
            'short_name':'CARPARK_SHORT_NAME',
            'icon_text':'CARPARK_ICON_TEXT',
        }.items() if key in env})


SETTINGS=DeploymentSettings.from_env()
