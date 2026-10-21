"""Private operator-approved, short-lived initial-passkey grants.

Only hashes of native login codes and browser access cookies are persisted.
A reserved finish remains blocked after an ambiguous failure or process exit.
"""
import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path


class EnrollmentStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('Enrollment database must not be a symlink')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=.2)
        self.db.execute('CREATE TABLE IF NOT EXISTS invitations('
                        'code_hash TEXT PRIMARY KEY,subject TEXT NOT NULL,expires INTEGER NOT NULL,'
                        'session_hash TEXT UNIQUE,state TEXT NOT NULL)')
        self.db.commit()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def approve(self, code, subject, expires, *, max_validity_seconds=1800):
        if not re.fullmatch('[A-Za-z0-9]{12}', code) or not isinstance(subject, str) or not subject:
            raise ValueError('Explicit long code and subject required')
        if type(max_validity_seconds) is not int or not 1 <= max_validity_seconds <= 86400:
            raise ValueError('Explicit validity bound must be at most 24 hours')
        if type(expires) is not int or expires > time.time() + max_validity_seconds:
            raise ValueError('Enrollment expiry exceeds the approved validity bound')
        with self.db:
            self.db.execute('DELETE FROM invitations WHERE expires<=?', (int(time.time()),))
            # Reissuing an invitation closes any earlier enrollment session.
            self.db.execute('DELETE FROM invitations WHERE subject=?', (subject,))
            self.db.execute('INSERT INTO invitations VALUES(?,?,?,NULL,?)',
                            (self.digest(code), subject, expires, 'pending'))

    def pending(self, code):
        row = self.db.execute('SELECT subject FROM invitations WHERE code_hash=? '
                              'AND state=? AND expires>?',
                              (self.digest(code), 'pending', int(time.time()))).fetchone()
        return row[0] if row else None

    def bind(self, code, subject, session):
        if not isinstance(session, str) or not session:
            return False
        with self.db:
            cursor = self.db.execute('UPDATE invitations SET session_hash=?,state=? '
                                     'WHERE code_hash=? AND subject=? AND state=? AND expires>?',
                                     (self.digest(session), 'active', self.digest(code), subject,
                                      'pending', int(time.time())))
            return cursor.rowcount == 1

    def subject(self, session):
        if not isinstance(session, str) or not session:
            return None
        row = self.db.execute('SELECT subject FROM invitations WHERE session_hash=? '
                              'AND state=? AND expires>?',
                              (self.digest(session), 'active', int(time.time()))).fetchone()
        return row[0] if row else None

    def reserve_finish(self, session):
        with self.db:
            cursor = self.db.execute('UPDATE invitations SET state=? WHERE session_hash=? '
                                     'AND state=? AND expires>?',
                                     ('finishing', self.digest(session), 'active', int(time.time())))
            return cursor.rowcount == 1

    def close_grant(self, session):
        with self.db:
            self.db.execute('UPDATE invitations SET state=? WHERE session_hash=?',
                            ('closed', self.digest(session)))

    def close(self):
        self.db.close()
