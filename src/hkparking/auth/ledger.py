"""Small durable grant ledger. No secrets, network calls, or long transactions."""
import hashlib
import secrets
import sqlite3
import time


class LimitReached(Exception):
    pass


class Ledger:
    POINTER_LIMIT = 4096
    FAMILY_LIMIT = 1024
    def __init__(self,path):
        self.db = sqlite3.connect(path,timeout=2,isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, client TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pointers(jti TEXT PRIMARY KEY, family TEXT NOT NULL, kind TEXT NOT NULL, spent INTEGER NOT NULL DEFAULT 0, exp INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS consumed_codes(hash TEXT PRIMARY KEY, exp INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS admission(kind TEXT NOT NULL, id TEXT NOT NULL, exp INTEGER, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY, client TEXT NOT NULL, exp REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS revoke_outbox(family TEXT PRIMARY KEY, generation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS provenance(family TEXT PRIMARY KEY, owner TEXT NOT NULL, native_client TEXT NOT NULL, native_grant TEXT NOT NULL);
        ''')

    def record(self,family,client,payloads,reserved=None,provenance=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('DELETE FROM pointers WHERE exp < ?',(int(time.time()),))
            self.db.execute('DELETE FROM families WHERE id NOT IN (SELECT family FROM pointers) AND id NOT IN (SELECT family FROM revoke_outbox)')
            pointer_count=self.db.execute('SELECT count(*) FROM pointers').fetchone()[0]
            family_count=self.db.execute('SELECT count(*) FROM families').fetchone()[0]
            existing=self.db.execute('SELECT 1 FROM families WHERE id=?',(family,)).fetchone()
            if pointer_count+len(payloads)>self.POINTER_LIMIT or (not existing and family_count>=self.FAMILY_LIMIT):
                raise LimitReached()
            if reserved is not None:
                row = self.lookup(reserved)
                if row is None or row['status']!='pending' or row['family']!=family:
                    raise ValueError('reservation no longer active')
            self.db.execute("INSERT OR IGNORE INTO families VALUES(?,?,'live')",(family,client))
            if provenance is not None:
                prior=self.provenance(family)
                if prior is not None and prior!=provenance:
                    raise ValueError('Grant provenance changed')
                self.db.execute('INSERT OR IGNORE INTO provenance VALUES(?,?,?,?)',(family,*provenance))
            for payload in payloads:
                self.db.execute('INSERT INTO pointers(jti,family,kind,exp) VALUES(?,?,?,?)',(payload['jti'],family,payload.get('token_use','access'),payload['exp']))
            if reserved is not None:
                self.db.execute("UPDATE families SET status='live' WHERE id=? AND status='pending'",(family,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def reserve(self,jti,client):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.lookup(jti)
            outcome = 'denied'
            if row is not None and row['client']==client and row['kind']=='refresh':
                if row['status']=='pending':
                    outcome = 'busy'
                elif row['status']=='live' and row['spent']:
                    outcome = 'replay'
                elif row['status']=='live':
                    self.db.execute('UPDATE pointers SET spent=1 WHERE jti=?',(jti,))
                    self.db.execute("UPDATE families SET status='pending' WHERE id=?",(row['family'],))
                    outcome = 'reserved'
            self.db.execute('COMMIT')
            return outcome
        except BaseException:
            self.db.execute('ROLLBACK')
            raise


    def lookup(self,jti):
        return self.db.execute('SELECT p.*,f.client,f.status FROM pointers p JOIN families f ON f.id=p.family WHERE p.jti=?',(jti,)).fetchone()

    def provenance(self,family):
        row=self.db.execute('SELECT owner,native_client,native_grant FROM provenance WHERE family=?',(family,)).fetchone()
        return tuple(row) if row is not None else None

    def revoke(self,family):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute("UPDATE families SET status='revoked' WHERE id=?",(family,))
            self.db.execute('INSERT INTO revoke_outbox VALUES(?,?) ON CONFLICT(family) DO UPDATE SET generation=excluded.generation',(family,secrets.token_hex(16)))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def consume_code(self,code,expires_at):
        self.db.execute('DELETE FROM consumed_codes WHERE exp < ?',(int(time.time()),))
        digest = hashlib.sha256(code.encode()).hexdigest()
        return self.db.execute('INSERT OR IGNORE INTO consumed_codes VALUES(?,?)',(digest,expires_at)).rowcount==1

    def admit(self,kind,key,cap,ttl):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            now = int(time.time())
            expired = [r[0] for r in self.db.execute('SELECT id FROM admission WHERE kind=? AND exp<?',(kind,now))]
            self.db.execute('DELETE FROM admission WHERE kind=? AND exp<?',(kind,now))
            count = self.db.execute('SELECT count(*) FROM admission WHERE kind=?',(kind,)).fetchone()[0]
            if count>=cap:
                raise LimitReached()
            self.db.execute('INSERT INTO admission VALUES(?,?,?)',(kind,key,now+ttl))
            self.db.execute('COMMIT')
            return expired
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def registered(self,client):
        return self.db.execute("SELECT 1 FROM admission WHERE kind='registration' AND id=? AND (exp IS NULL OR exp>=?)",(client,int(time.time()))).fetchone() is not None

    def admit_transaction(self,key,client,deadline):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            expired = [r[0] for r in self.db.execute('SELECT id FROM transactions WHERE exp<=?',(time.time(),))]
            self.db.execute('DELETE FROM transactions WHERE exp<=?',(time.time(),))
            if not self.db.execute('SELECT 1 FROM transactions WHERE id=?',(key,)).fetchone():
                total = self.db.execute('SELECT count(*) FROM transactions').fetchone()[0]
                own = self.db.execute('SELECT count(*) FROM transactions WHERE client=?',(client,)).fetchone()[0]
                if total>=64 or own>=64:
                    raise LimitReached()
                self.db.execute('INSERT INTO transactions VALUES(?,?,?)',(key,client,deadline))
            self.db.execute('COMMIT')
            return expired
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def approved(self,client):
        self.db.execute("UPDATE admission SET exp=NULL WHERE kind='registration' AND id=?",(client,))
