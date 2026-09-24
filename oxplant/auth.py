"""Console authentication: PBKDF2 password hashing, sessions, lockout, roles."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, List, Optional

ROLES: Dict[str, List[str]] = {
    "admin": ["view", "ack_alerts", "resolve_alerts", "review_changes", "approve_assets", "manage"],
    "engineer": ["view", "ack_alerts", "review_changes", "approve_assets"],
    "soc": ["view", "ack_alerts", "resolve_alerts"],
    "viewer": ["view"],
}
LOCKOUT_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


def hash_password(password: str, iterations: int = 600_000, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_hex = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class Session:
    def __init__(self, username: str, role: str, ip: str, ttl: float):
        self.token = secrets.token_urlsafe(32)
        self.username = username
        self.role = role
        self.ip = ip
        self.created = time.time()
        self.expires = self.created + ttl
        self.last_seen = self.created

    def can(self, permission: str) -> bool:
        return permission in ROLES.get(self.role, [])


class Authenticator:
    def __init__(self, users: Dict[str, tuple], session_ttl: float = 8 * 3600):
        """users: {username: (role, password_hash)}"""
        self.users = users
        self.ttl = session_ttl
        self.sessions: Dict[str, Session] = {}
        self.failures: Dict[str, List[float]] = {}
        self.lock = threading.Lock()
        # dummy hash with the same cost as the real ones so unknown users take the same time to reject
        iterations = max([int(h.split("$")[1]) for _, h in users.values() if h.count("$") == 3] or [600_000])
        self._dummy = hash_password(secrets.token_hex(8), iterations)

    def locked_until(self, key: str) -> float:
        hits = [t for t in self.failures.get(key, []) if time.time() - t < LOCKOUT_SECONDS]
        self.failures[key] = hits
        return hits[-1] + LOCKOUT_SECONDS if len(hits) >= LOCKOUT_ATTEMPTS else 0.0

    def login(self, username: str, password: str, ip: str) -> tuple:
        """Returns (session or None, reason)."""
        key = f"{username}@{ip}"
        with self.lock:
            until = self.locked_until(key)
            if until:
                return None, f"locked for {int(until - time.time())} s"
            user = self.users.get(username)
        # the expensive PBKDF2 check runs outside the lock; unknown users cost the same as real ones
        ok = verify_password(password, user[1] if user else self._dummy)
        with self.lock:
            if not user or not ok:
                self.failures.setdefault(key, []).append(time.time())
                left = LOCKOUT_ATTEMPTS - len(self.failures[key])
                return None, ("invalid credentials" if left > 0 else "account locked")
            self.failures.pop(key, None)
            s = Session(username, user[0], ip, self.ttl)
            self.sessions[s.token] = s
            return s, "ok"

    def get(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        with self.lock:
            s = self.sessions.get(token)
            if s and s.expires < time.time():
                del self.sessions[token]
                return None
            if s:
                s.last_seen = time.time()
            return s

    def logout(self, token: Optional[str]) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)

    def failure_count(self, username: str, ip: str) -> int:
        return len(self.failures.get(f"{username}@{ip}", []))
