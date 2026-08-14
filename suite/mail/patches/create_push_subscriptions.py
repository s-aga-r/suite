import time

import frappe
from frappe import _

from suite.mail.jmap import get_user_context
from suite.mail.utils import log_mail_error


def execute() -> None:
    if not frappe.utils.get_url().startswith("https://"):
        return

    for user in frappe.db.get_all("User Settings", {"username": ["!=", ""]}, pluck="user"):
        try:
            ctx = get_user_context(user, ignore_permissions=True)

            subscriptions = ctx.get_all("PushSubscription")
            if ids := [s["id"] for s in subscriptions]:
                ctx.destroy("PushSubscription", ids)

            ps = frappe.new_doc("Push Subscription")
            ps.user = user
            ps.insert(ignore_permissions=True)
        except Exception as e:
            log_mail_error(
                _("Push Subscription Creation Failed"),
                _("Failed to create push subscription for user {0}: {1}").format(user, str(e)),
            )

        time.sleep(0.1)
