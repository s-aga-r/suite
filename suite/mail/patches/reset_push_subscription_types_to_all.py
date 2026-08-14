import frappe
from frappe import _

from suite.mail.jmap import get_user_context
from suite.mail.utils import log_mail_error
from suite.mail.utils.user import get_jmap_configured_users


def execute() -> None:
    """Reset every push subscription's types to null, subscribing it to all types.

    The JMAP server only pushes CalendarAlert notifications (triggered event alerts) to
    subscriptions whose types include it. Subscriptions used to be created with an explicit
    list ("Email", "Mailbox", "Identity", "VacationResponse") that predates calendar alert
    support and would never receive them. Null means all supported types — including
    CalendarAlert and any types added later — which is now also the default for new
    subscriptions.
    """

    if not frappe.utils.get_url().startswith("https://"):
        return

    for user in get_jmap_configured_users():
        try:
            ctx = get_user_context(user, ignore_permissions=True)

            payload = {
                subscription["id"]: {"types": None} for subscription in ctx.get_all("PushSubscription")
            }
            if not payload:
                continue

            response = ctx.update("PushSubscription", payload)
            if not_updated := response.get("notUpdated"):
                errors = "<br>".join(
                    # SetError's description is optional; fall back to its required type
                    f"{id}: {error.get('description') or error.get('type')}"
                    for id, error in not_updated.items()
                )
                log_mail_error(
                    _("Push Subscription Update Failed"),
                    _("Failed to reset push subscription types for user {0}:<br>{1}").format(user, errors),
                )
        except Exception as e:
            log_mail_error(
                _("Push Subscription Update Failed"),
                _("Failed to reset push subscription types for user {0}: {1}").format(user, str(e)),
            )
