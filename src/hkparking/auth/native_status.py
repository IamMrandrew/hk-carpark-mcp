"""Pinned v2.17.0 status contract, derived from fresh publisher-verified native DDL.
Read-only, WAL-aware, no schema discovery/pinning from the configured runtime DB.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

RELEASE='v2.17.0'
_CONTRACT=json.loads((Path(__file__).parent/'native-schema-v2.17.0.json').read_text())
_SCHEMA=tuple(tuple(row) for row in _CONTRACT['schema'])
_MIGRATIONS=tuple(tuple(row) for row in _CONTRACT['migrations']['schema_migrations'])


class NativeStatus:
    def __init__(self,config):
        self._config=config

    @contextmanager
    def snapshot(self):
        cfg=self._config
        if cfg.native_release!=RELEASE or cfg.identity_file is None or not cfg.identity_database:
            raise ValueError('Approved native status source required')
        path=Path(cfg.identity_database)
        if not path.is_absolute() or path.is_symlink() or path.resolve()!=path:
            raise ValueError('Exact approved status path required')
        def bound():
            st=path.stat()
            if (st.st_dev,st.st_ino)!=cfg.identity_file:
                raise ValueError('Native status source replaced')
        bound()
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            schema=tuple(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
            if schema!=_SCHEMA or db.execute('PRAGMA user_version').fetchone()[0]!=_CONTRACT['user_version']:
                raise ValueError('Native schema incompatible')
            if tuple(db.execute('SELECT version,dirty FROM schema_migrations'))!=_MIGRATIONS:
                raise ValueError('Native migration incompatible')
            bound()
            yield db
            bound()
        finally:
            db.close()

    def enabled(self):
        with self.snapshot() as db:
            return db.execute('SELECT disabled FROM users WHERE id=?',(self._config.allowed_sub,)).fetchall()==[(0,)]

    def client_policy(self,db):
        row=db.execute('SELECT callback_urls,is_public,pkce_enabled,client_type FROM oidc_clients WHERE id=?',(self._config.client_id,)).fetchone()
        return row is not None and json.loads(row[0])==['https://carpark.laporatory.com/auth/callback'] and row[1:]==(0,1,'standard')

    def grant(self,token,claims):
        with self.snapshot() as db:
            if db.execute('SELECT disabled FROM users WHERE id=?',(self._config.allowed_sub,)).fetchall()!=[(0,)] or not self.client_policy(db):
                return None
            # Native Store keys JWT access sessions by signature (verified with a
            # fresh actual native token); request_id is the stable grant identity.
            rows=db.execute("SELECT request_id,client_id,request_data FROM oauth2_sessions WHERE kind='access_token' AND key=? AND active=1",(token.rsplit('.',1)[1],)).fetchall()
            if len(rows)!=1:
                return None
            grant,client,raw=rows[0]
            data=json.loads(raw)
            if (not isinstance(grant,str) or not grant or client!=self._config.client_id
                    or data.get('client_id')!=client or data.get('id')!=grant
                    or data.get('session',{}).get('subject')!=claims['sub']
                    or data.get('granted_scope')!=['openid']):
                return None
            return hashlib.sha256(grant.encode()).hexdigest()

    def interaction(self,iid):
        with self.snapshot() as db:
            if not self.client_policy(db):
                return None
            rows=db.execute('SELECT client_id,scopes,user_id,parameters FROM interaction_sessions WHERE id=?',(iid,)).fetchall()
            if len(rows)!=1:
                return None
            client,scopes,owner,parameters=rows[0]
            if client!=self._config.client_id or json.loads(scopes)!=['openid'] or owner not in (None,self._config.allowed_sub):
                return None
            return json.loads(parameters)

    def refresh(self,token,expected):
        return self.refresh_expiry(token,expected) is not None

    def refresh_expiry(self,token,expected=None):
        """Return the current native deadline after checking identity and grant state."""
        if not isinstance(token,str) or not token or len(token)>16384:
            return None
        with self.snapshot() as db:
            if (expected is not None and expected[:2]!=(self._config.allowed_sub,self._config.client_id)
                    or not self.client_policy(db)):
                return None
            if db.execute('SELECT disabled FROM users WHERE id=?',(self._config.allowed_sub,)).fetchall()!=[(0,)]:
                return None
            # Pinned v2.17.0 binary stores oauth2_sessions.expires_at as INTEGER
            # Unix epoch seconds (verified on the publisher-verified fixture).
            # unixepoch() on a bare INTEGER treats it as a Julian day number and
            # returns NULL, which would silently drop every live refresh row.
            # TEXT timestamps (if ever written) still parse via unixepoch();
            # NULL/invalid values stay NULL and are filtered out (fail closed).
            rows=db.execute("SELECT request_id,client_id,request_data,CASE typeof(expires_at) WHEN 'integer' THEN expires_at ELSE unixepoch(expires_at) END FROM oauth2_sessions WHERE kind='refresh_token' AND key=? AND active=1 AND expires_at IS NOT NULL AND (CASE typeof(expires_at) WHEN 'integer' THEN expires_at ELSE unixepoch(expires_at) END)>unixepoch('now')",(token.rsplit('.',1)[-1],)).fetchall()
            if len(rows)!=1:
                return None
            grant,client,raw,deadline=rows[0];data=json.loads(raw)
            valid = (isinstance(grant,str) and bool(grant) and client==self._config.client_id
                and data.get('client_id')==client and data.get('id')==grant
                and data.get('session',{}).get('subject')==self._config.allowed_sub
                and data.get('granted_scope')==['openid']
                and (expected is None or hashlib.sha256(grant.encode()).hexdigest()==expected[2]))
            return deadline if valid else None
