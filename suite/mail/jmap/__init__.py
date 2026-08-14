"""Frappe-side helpers for working with the JMAP server through jmaplib.

This package is not a JMAP implementation — jmaplib owns the protocol. It provides:

- :func:`get_jmap_connection` / :func:`get_context`: per-user connections (credentials, Redis
  session reuse, permission gates) and account-scoped working contexts.
- Domain helper modules (:mod:`~suite.mail.jmap.mail`, :mod:`~suite.mail.jmap.contacts`,
  :mod:`~suite.mail.jmap.calendars`) with payload shaping for each JMAP data type.
- The account-keyed convenience lookups and whitelisted endpoints below.
"""

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.caching import request_cache

from suite.mail.doctype.user_account.user_account import get_user_for_jmap_account
from suite.mail.jmap.client import JMAPConnection, JMAPConnectionInfo, JMAPSessionManager
from suite.mail.jmap.context import JMAPContext, format_jmap_error, get_jmap_set_error_message
from suite.mail.store import Entity, get_data_store
from suite.mail.utils import get_config
from suite.utils.user import is_system_manager


@request_cache
def get_jmap_connection(
    user: str, ignore_permissions: bool = False, timeout: tuple[float, float] = (30.0, 60.0)
) -> JMAPConnection:
    """Returns a JMAPConnection instance for the specified user, using the user's settings for connection details.

    Cached per request so the many callers that resolve a connection for the same user reuse one
    instance (and skip the repeated password decryption / session lookup).
    """

    if not ignore_permissions:
        if user != frappe.session.user and not is_system_manager(frappe.session.user):
            frappe.throw(
                _("You do not have permission to access the JMAPConnection for user {0}.").format(
                    frappe.bold(user)
                ),
                frappe.PermissionError,
            )

    if not frappe.get_cached_value("User", user, "enabled"):
        frappe.throw(_("User {0} does not exist or is disabled.").format(frappe.bold(user)))

    settings = frappe.db.exists("User Settings", {"user": user, "username": ["!=", None]})
    if not settings:
        frappe.throw(_("User {0} does not have JMAP settings configured.").format(frappe.bold(user)))

    user_settings = frappe.get_cached_doc("User Settings", settings)
    server_url, verify_ssl = get_config(("server_url", "verify_ssl"))

    return JMAPConnection(
        JMAPConnectionInfo(
            server_url,
            user_settings.username,
            user_settings.get_password("app_password"),
            timeout,
            verify_ssl=bool(verify_ssl),
        ),
        session_manager=get_jmap_session_manager(user),
        user=user,
    )


def get_jmap_session_manager(user) -> JMAPSessionManager:
    """Returns a JMAPSessionManager instance for the specified user, using the data store for session management."""

    return JMAPSessionManager(
        get_session=lambda: frappe.cache.hget("jmap:sessions", user),
        set_session=lambda session: frappe.cache.hset("jmap:sessions", user, session),
        clear_session=lambda: frappe.cache.hdel("jmap:sessions", user),
    )


def get_context(
    account: str,
    ignore_permissions: bool = False,
    timeout: tuple[float, float] = (30.0, 60.0),
) -> JMAPContext:
    """Returns a JMAP working context scoped to the given JMAP Account."""

    user = get_user_for_jmap_account(account, raise_exception=True)
    connection = get_jmap_connection(user, ignore_permissions=ignore_permissions, timeout=timeout)
    return JMAPContext(connection, account)


def get_user_context(user: str, ignore_permissions: bool = False) -> JMAPContext:
    """Returns a user-scoped JMAP working context (no accountId — e.g. for push subscriptions)."""

    connection = get_jmap_connection(user, ignore_permissions=ignore_permissions)
    return JMAPContext(connection, None)


def invalidate_jmap_identities_cache(account: str) -> None:
    """Invalidates the JMAP identities cache for the specified account."""

    store = get_data_store(account)
    store.delete_all(Entity.IDENTITY)


def invalidate_jmap_mailboxes_cache(account: str) -> None:
    """Invalidates the JMAP mailboxes cache for the specified account."""

    store = get_data_store(account)
    store.delete_all(Entity.MAILBOX)


def get_identities(account: str) -> list[dict]:
    """Returns the list of identities for the specified account."""

    user = get_user_for_jmap_account(account, raise_exception=True)
    ctx = JMAPContext(get_jmap_connection(user), account)

    identities = [
        {
            "name": f"{account}|{i['id']}",
            "account": account,
            "user": user,
            "id": i["id"],
            "_name": i["name"],
            "email": i["email"].lower(),
            "bcc": [{"display_name": b["name"], "email": b["email"].lower()} for b in i.get("bcc") or []],
            "reply_to": [
                {"display_name": r["name"], "email": r["email"].lower()} for r in i.get("replyTo") or []
            ],
            "html_signature": i["htmlSignature"],
            "text_signature": i["textSignature"],
            "may_delete": cint(i["mayDelete"]),
        }
        for i in ctx.identities
    ]

    return identities


def get_participant_identities(account: str) -> list[dict]:
    """Returns the list of participant identities for the specified account."""

    user = get_user_for_jmap_account(account, raise_exception=True)
    ctx = JMAPContext(get_jmap_connection(user), account)

    return [
        {
            "name": f"{account}|{i['id']}",
            "account": account,
            "user": user,
            "id": i["id"],
            "_name": i["name"],
            "email": i["calendarAddress"].lower().replace("mailto:", ""),
            "default": cint(bool(i["isDefault"])),
        }
        for i in ctx.get_all("ParticipantIdentity")
    ]


def get_identity_id_by_email(account: str, email: str, raise_exception: bool = False) -> str | None:
    """Returns the identity ID for the specified email address, or None if not found."""

    from suite.mail.jmap.mail import get_identity_id_by_email as _get_identity_id_by_email

    return _get_identity_id_by_email(get_context(account), email, raise_exception=raise_exception)


def get_mailboxes(account: str) -> list[dict]:
    """Returns the list of mailboxes for the specified account."""

    user = get_user_for_jmap_account(account, raise_exception=True)
    ctx = JMAPContext(get_jmap_connection(user), account)

    mailboxes = [
        {
            "name": f"{account}|{m['id']}",
            "account": account,
            "user": user,
            "id": m["id"],
            "role": m["role"],
            "_name": m["name"],
            "_parent": f"{account}|{m['parentId']}" if m.get("parentId") else None,
            "parent_id": m["parentId"],
            "subscribed": m["isSubscribed"],
        }
        for m in ctx.mailboxes
    ]

    return mailboxes


def get_mailbox_id_by_role(
    account: str,
    role: str,
    create_if_not_exists: bool = False,
    raise_exception: bool = False,
) -> str | None:
    """Returns the mailbox ID for the specified role, or None if not found. Optionally creates the mailbox if it does not exist."""

    from suite.mail.jmap.mail import get_mailbox_id_by_role as _get_mailbox_id_by_role

    return _get_mailbox_id_by_role(
        get_context(account), role, create_if_not_exists=create_if_not_exists, raise_exception=raise_exception
    )


def get_mailbox_role_by_id(account: str, id: str, raise_exception: bool = False) -> str | None:
    """Returns the mailbox role for the specified mailbox ID, or None if not found."""

    from suite.mail.jmap.mail import get_mailbox_role_by_id as _get_mailbox_role_by_id

    return _get_mailbox_role_by_id(get_context(account), id, raise_exception=raise_exception)


def get_mailbox_name_by_id(account: str, id: str, raise_exception: bool = False) -> str | None:
    """Returns the mailbox name for the specified mailbox ID, or None if not found."""

    from suite.mail.jmap.mail import get_mailbox_name_by_id as _get_mailbox_name_by_id

    return _get_mailbox_name_by_id(get_context(account), id, raise_exception=raise_exception)


def get_mailbox_id_by_name(account: str, name: str, raise_exception: bool = False) -> str | None:
    """Returns the mailbox ID for the specified mailbox name, or None if not found."""

    from suite.mail.jmap.mail import get_mailbox_id_by_name as _get_mailbox_id_by_name

    return _get_mailbox_id_by_name(get_context(account), name, raise_exception=raise_exception)


def get_default_address_book_id(account: str, raise_exception: bool = False) -> str | None:
    """Returns the ID of the default address book for the specified account, or None if not found."""

    from suite.mail.jmap.contacts import get_default_address_book_id as _get_default_address_book_id

    return _get_default_address_book_id(get_context(account), raise_exception=raise_exception)


def get_default_calendar_id(account: str, raise_exception: bool = False) -> str | None:
    """Returns the ID of the default calendar for the specified account, or None if not found."""

    from suite.mail.jmap.calendars import get_default_calendar_id as _get_default_calendar_id

    return _get_default_calendar_id(get_context(account), raise_exception=raise_exception)


@frappe.whitelist()
def get_user_accounts(user: str) -> list[str]:
    """Returns a list of account names for the specified user."""

    if user != frappe.session.user and not is_system_manager(frappe.session.user):
        frappe.throw(
            _("Not permitted to view accounts for user {0}.").format(frappe.bold(user)),
            frappe.PermissionError,
        )

    from suite.mail.doctype.user_account.user_account import get_user_jmap_accounts

    return get_user_jmap_accounts(user)


@frappe.whitelist()
def get_user_account_ids(user: str) -> list[str]:
    """Returns the JMAP account IDs the specified user has access to."""

    if user != frappe.session.user and not is_system_manager(frappe.session.user):
        frappe.throw(
            _("Not permitted to view accounts for user {0}.").format(frappe.bold(user)),
            frappe.PermissionError,
        )

    from suite.mail.doctype.user_account.user_account import get_user_jmap_accounts

    return get_user_jmap_accounts(user)


@frappe.whitelist()
def get_mailboxes_for_account(account: str) -> list[dict]:
    """Returns the list of mailboxes for the specified account."""

    return get_mailboxes(account)
