"""Credential-only DEMO authentication; no LIVE config/session lookup."""

import base64
import asyncio
from collections import OrderedDict
import hashlib
import hmac
import json
import secrets
import time

from .config import encoded_password_hash
from .domain.models import Principal


class DemoAuth:
    def __init__(self, config, database, *, clock=time.time):
        self.config, self.database, self.clock = config, database, clock
        self._attempts = OrderedDict()
        self._validation_lock=asyncio.Lock()

    def configured(self):
        return bool(self.config.login_username and len(self.config.session_secret)>=32 and
            encoded_password_hash(self.config.login_password_hash))

    def _credentials(self):
        return {self.config.login_username:self.config.login_password_hash, **dict(self.config.additional_users)}

    def _user_fingerprint(self, username):
        value=username+"\0"+self._credentials()[username]
        return hmac.new(self.config.session_secret.encode(),value.encode(),hashlib.sha256).hexdigest()

    def _fingerprint(self):
        value=self.config.login_username+"\0"+self.config.login_password_hash
        return hmac.new(self.config.session_secret.encode(),value.encode(),hashlib.sha256).hexdigest()

    def initialize(self):
        if not self.configured():
            return
        with self.database.transaction() as conn:
            row=conn.execute("SELECT value FROM demo_v2_meta WHERE key='auth_fingerprint'").fetchone()
            current=self._fingerprint()
            if not row or not hmac.compare_digest(row[0],current):
                conn.execute("UPDATE demo_v2_meta SET value=CAST(value AS INTEGER)+1 WHERE key='session_version'")
                conn.execute("INSERT OR REPLACE INTO demo_v2_meta VALUES ('auth_fingerprint',?)",(current,))

    def version(self):
        with self.database.connect() as conn:
            return conn.execute("SELECT value FROM demo_v2_meta WHERE key='session_version'").fetchone()[0]

    def revoke(self):
        # Logout revokes all DEMO sessions, durably across process restart.
        with self.database.transaction() as conn:
            conn.execute("UPDATE demo_v2_meta SET value=CAST(value AS INTEGER)+1 WHERE key='session_version'")

    def rate_limited(self, key):
        now=self.clock()
        attempts=[stamp for stamp in self._attempts.pop(key,[]) if 0 <= now-stamp < 60]
        limited=len(attempts)>=5
        attempts.append(now)
        self._attempts[key]=attempts[-5:]
        while len(self._attempts)>1000:
            self._attempts.popitem(last=False)
        return limited

    async def validate(self, username, password):
        # Argon2 is CPU/memory work; never pause the public-data event loop.
        async with self._validation_lock:
            work=asyncio.create_task(asyncio.to_thread(self._validate,username,password))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # Keep the memory-heavy verifier serialized even if its HTTP
                # caller disconnects while the native hash is still running.
                await work
                raise

    def _validate(self, username, password):
        if not self.configured():
            return None
        credentials=self._credentials()
        # Unknown users still pay the same serialized password verification cost.
        expected=credentials.get(username,self.config.login_password_hash)
        valid=False
        if expected.startswith("$argon2id$"):
            try:
                from argon2 import PasswordHasher
                valid=PasswordHasher().verify(expected,password)
            except Exception:
                valid=False
        elif expected.startswith("sha256:"):
            digest="sha256:"+hashlib.sha256(password.encode()).hexdigest()
            valid=hmac.compare_digest(digest,expected)
        username_valid=username in credentials
        return Principal(username) if valid and username_valid else None

    def create_session(self, username):
        if not self.configured() or username not in self._credentials():
            raise ValueError("DEMO auth is not configured")
        payload=json.dumps({"sub":username,"iat":int(self.clock()),"version":self.version(),
            "credential":self._user_fingerprint(username),
            "nonce":secrets.token_urlsafe(24),"aud":"DEMO_V2"},sort_keys=True,separators=(",",":")).encode()
        encoded=base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature=hmac.new(self.config.session_secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
        return encoded+"."+signature

    def verify(self, token):
        if not self.configured() or not token or len(token)>4096:
            return None
        try:
            encoded,signature=token.split(".")
            expected=hmac.new(self.config.session_secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature,expected):
                return None
            payload=json.loads(base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)))
            age=self.clock()-payload["iat"]
            if (payload["aud"]!="DEMO_V2" or payload["sub"] not in self._credentials() or
                    not 0 <= age <= self.config.session_ttl_seconds or payload["version"]!=self.version()):
                return None
            credential=payload.get("credential")
            if credential is None:
                # Preserve existing primary-account sessions across adding a user.
                if payload["sub"]!=self.config.login_username:
                    return None
            elif not isinstance(credential,str) or not hmac.compare_digest(credential,self._user_fingerprint(payload["sub"])):
                return None
            return Principal(payload["sub"])
        except (ValueError,KeyError,TypeError,UnicodeError):
            return None

    def csrf(self, token):
        return hmac.new(self.config.session_secret.encode(),("demo-csrf:"+token).encode(),hashlib.sha256).hexdigest()

    def verify_csrf(self, token, supplied):
        return bool(token and supplied and hmac.compare_digest(self.csrf(token).encode(),supplied.encode()))
