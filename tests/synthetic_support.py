"""Explicit SYNTHETIC mock DBs from checksum-verified fresh native v2.17.0 DDL.
No retained runtime DB, tokens, account data or key material are copied.
"""
import json
import sqlite3
from pathlib import Path

CONTRACT=json.loads((Path(__file__).resolve().parents[1]/'src/hkparking/auth/native-schema-v2.17.0.json').read_text())


def identity_db(path,sub='fixture-sub',client='fixture-client'):
    with sqlite3.connect(path) as db:
        for kind in ('table','index','view','trigger'):
            for typ,name,table,sql in CONTRACT['schema']:
                if typ==kind and sql:
                    db.execute(sql)
        for row in CONTRACT['migrations']['schema_migrations']:
            db.execute('INSERT INTO schema_migrations VALUES(?,?)',row)
        db.execute('INSERT INTO users(id,username,first_name,last_name,display_name,disabled) VALUES(?,?,?,?,?,0)',(sub,'SYNTHETIC-user','SYNTHETIC','','SYNTHETIC'))
        db.execute('INSERT INTO oidc_clients(id,created_at,name,callback_urls,is_public,pkce_enabled) VALUES(?,CURRENT_TIMESTAMP,?,?,0,1)',(client,'SYNTHETIC-client',json.dumps(['https://carpark.laporatory.com/auth/callback'])))
    return path


def binding(path):
    st=Path(path).stat()
    return (st.st_dev,st.st_ino)


def seed_grant(path,token,claims,grant='SYNTHETIC-native-grant'):
    if not isinstance(claims.get('client_id'),str) or not isinstance(claims.get('jti'),str):
        return
    data={'id':grant,'client_id':claims['client_id'],'granted_scope':['openid'],'session':{'subject':claims['sub'],'jwt_claims':{'JTI':claims['jti']}}}
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR REPLACE INTO oauth2_sessions(id,created_at,kind,key,request_id,client_id,active,request_data) VALUES(?,CURRENT_TIMESTAMP,'access_token',?,?,?,1,?)",('SYNTHETIC-session-'+claims['jti'],token.rsplit('.',1)[1],grant,claims['client_id'],json.dumps(data)))
