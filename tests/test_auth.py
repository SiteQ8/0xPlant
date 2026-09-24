from oxplant.auth import Authenticator, LOCKOUT_ATTEMPTS, hash_password, verify_password


def test_hash_and_verify():
    h = hash_password("Plant@2025", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$")
    assert verify_password("Plant@2025", h) and not verify_password("plant@2025", h)
    assert not verify_password("x", "garbage")


def test_login_sessions_roles_and_lockout():
    a = Authenticator({"admin": ("admin", hash_password("pw", 1000)), "viewer": ("viewer", hash_password("pw", 1000))}, session_ttl=60)
    s, reason = a.login("admin", "pw", "10.0.0.1")
    assert s and reason == "ok" and s.can("manage") and a.get(s.token) is s
    v, _ = a.login("viewer", "pw", "10.0.0.1")
    assert v.can("view") and not v.can("ack_alerts")
    a.logout(s.token)
    assert a.get(s.token) is None
    for i in range(LOCKOUT_ATTEMPTS):
        s, reason = a.login("admin", "bad", "10.0.0.2")
        assert s is None
    assert reason == "account locked"
    s, reason = a.login("admin", "pw", "10.0.0.2")
    assert s is None and reason.startswith("locked")
    s, reason = a.login("admin", "pw", "10.0.0.3")          # lockout is per user+ip
    assert s is not None
    s, reason = a.login("ghost", "pw", "10.0.0.3")
    assert s is None and reason == "invalid credentials"
