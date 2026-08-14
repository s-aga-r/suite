import hashlib

import frappe
from frappe.utils.caching import request_cache

from suite.mail.stalwart.transport import JMAPConnection, JMAPConnectionInfo, JMAPSessionManager
from suite.mail.utils import get_config, is_stalwart_configured

# Cache key under which the discovered management JMAP session is stored.
MANAGEMENT_SESSION_CACHE_KEY = "stalwart:management:sessions"


def _session_manager(cache_key: str) -> JMAPSessionManager:
    """Returns a session manager that persists the discovered session in Frappe's cache.

    Sessions are keyed by a hash of the server URL and username so that switching servers or
    admin credentials never reuses a stale session.
    """

    return JMAPSessionManager(
        get_session=lambda: frappe.cache.hget(MANAGEMENT_SESSION_CACHE_KEY, cache_key),
        set_session=lambda session: frappe.cache.hset(MANAGEMENT_SESSION_CACHE_KEY, cache_key, session),
        clear_session=lambda: frappe.cache.hdel(MANAGEMENT_SESSION_CACHE_KEY, cache_key),
    )


def _build_connection(username: str, cache_key: str | None, timeout: tuple[float, float]) -> JMAPConnection:
    """Builds a JMAPConnection for ``username`` using the configured admin password.

    ``cache_key`` persists the discovered session; pass ``None`` to always re-discover it.
    """

    is_stalwart_configured(raise_exception=True)

    server_url, password, verify_ssl = get_config(("server_url", "password", "verify_ssl"))

    return JMAPConnection(
        JMAPConnectionInfo(
            server_url,
            username,
            password,
            timeout,
            verify_ssl=bool(verify_ssl),
        ),
        session_manager=_session_manager(cache_key) if cache_key else None,
    )


@request_cache
def get_management_connection(timeout: tuple[float, float] = (30.0, 60.0)) -> JMAPConnection:
    """Returns a JMAPConnection authenticated as the Stalwart administrator.

    Used for all server-scoped directory management (Accounts, Groups, Mailing Lists, Roles,
    Domains, OAuth). Cached per request so the many service factories reuse one instance.
    """

    server_url, username = get_config(("server_url", "username"))
    cache_key = hashlib.sha1(f"{server_url}|{username}".encode()).hexdigest()

    return _build_connection(username, cache_key, timeout)


@request_cache
def get_account_management_connection(
    account: str, timeout: tuple[float, float] = (30.0, 60.0)
) -> JMAPConnection:
    """Returns a JMAPConnection authenticated as ``account`` via Stalwart master-user auth.

    Stalwart lets an administrator authenticate as any account by logging in with the username
    ``<account>%<admin_username>`` and the admin password. This is required for account-scoped
    management objects such as App Passwords, which are created within the target account.

    The session is deliberately not persisted. It carries the account's id, and that id changes
    whenever an account is deleted and recreated on the same address — a cached session then scopes
    every call to the id of the account that is gone, which the server rejects with "You are not an
    owner of account <id>". These connections are short-lived (one app password when an account is
    set up) and ``request_cache`` already shares them within a request, so rediscovering costs a
    single request and keeps the account id honest.
    """

    admin_username = get_config("username")

    return _build_connection(f"{account}%{admin_username}", None, timeout)
