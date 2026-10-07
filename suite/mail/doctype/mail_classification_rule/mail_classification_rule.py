# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document

from suite.mail.classification.category import Category
from suite.mail.classification.rules import load_default_rules
from suite.mail.doctype.user_account.user_account import get_user_jmap_accounts
from suite.mail.utils.validation import normalize_screened_value, validate_screened_value
from suite.utils.user import is_suite_admin, is_system_manager


class MailClassificationRule(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        account: DF.Link | None
        category: DF.Literal["Primary", "Promotions", "Social", "Updates", "Forums"]
        enabled: DF.Check
        is_default: DF.Check
        sender: DF.Data
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    def validate(self) -> None:
        self.validate_site_rule_permission()
        self.validate_default_rule()
        self.validate_sender()
        self.validate_duplicate_sender()

    def on_trash(self) -> None:
        if self.is_default and not self.flags.syncing_defaults:
            frappe.throw(_("A default rule cannot be deleted. Disable it instead."))

    def validate_site_rule_permission(self) -> None:
        """A rule without an account applies to every account, so only admins may manage it."""

        if self.account or self.flags.syncing_defaults:
            return

        user = frappe.session.user
        if not (is_system_manager(user) or is_suite_admin(user)):
            frappe.throw(
                _("Only a System Manager or Suite Admin can manage site-wide classification rules."),
                frappe.PermissionError,
            )

    def validate_default_rule(self) -> None:
        """A default rule is the app's to write. A site may switch one off, and nothing else."""

        if self.flags.syncing_defaults:
            return

        was_default = 0 if self.is_new() else self.get_doc_before_save().is_default
        if self.is_default != was_default or (self.is_default and self.has_value_changed("category")):
            frappe.throw(
                _(
                    "Default rules are managed by the app. Disable the rule, or add one of your own for the same sender."
                )
            )

    def validate_sender(self) -> None:
        """Normalise and validate the sender: a full email address or an '@domain' entry."""

        self.sender = normalize_screened_value(self.sender).lower()
        validate_screened_value(self.sender, raise_exception=True)

    def validate_duplicate_sender(self) -> None:
        """One rule per sender in each place a rule comes from: an account, the site, the app.

        The unique index on (account, sender, is_default) does not cover site-wide and default
        rules: MariaDB allows any number of rows with a NULL in a unique key.
        """

        if frappe.db.exists(
            "Mail Classification Rule",
            {
                "account": self.account or ("is", "not set"),
                "sender": self.sender,
                "is_default": self.is_default,
                "name": ["!=", self.name],
            },
        ):
            frappe.throw(_("There is already a rule for {0} here.").format(frappe.bold(self.sender)))


def remember_senders(account: str, senders: list[str], category: Category) -> None:
    """Have future mail from each of `senders` given `category` in `account`.

    A sender the account already has a rule for has that rule changed, and switched back on.
    """

    for sender in dict.fromkeys(sender.lower() for sender in senders if sender):
        name = frappe.db.exists("Mail Classification Rule", {"account": account, "sender": sender})
        rule = (
            frappe.get_doc("Mail Classification Rule", name)
            if name
            else frappe.new_doc("Mail Classification Rule", account=account, sender=sender)
        )
        rule.category = category.value.title()
        rule.enabled = 1
        rule.save()


def sync_default_rules() -> None:
    """Bring the site's default rules in step with the ones the app ships.

    A rule the app has gained is added, one whose category it changed is changed, and one it has
    dropped is deleted. Whether a rule is enabled is the site's choice and is left alone, as are
    the rules the site and its accounts wrote themselves.
    """

    shipped = {rule["sender"].lower(): rule["category"] for rule in load_default_rules()}
    existing = frappe.get_all(
        "Mail Classification Rule", filters={"is_default": 1}, fields=["name", "sender", "category"]
    )

    for row in existing:
        category = shipped.pop(row.sender, None)
        if category == row.category:
            continue

        rule = frappe.get_doc("Mail Classification Rule", row.name)
        rule.flags.syncing_defaults = True
        if category:
            rule.category = category
            rule.save(ignore_permissions=True)
        else:
            rule.delete(ignore_permissions=True)

    for sender, category in shipped.items():
        rule = frappe.new_doc("Mail Classification Rule", sender=sender, category=category, is_default=1)
        rule.flags.syncing_defaults = True
        rule.insert(ignore_permissions=True)


def get_permission_query_condition(user: str | None = None) -> str | None:
    user = user or frappe.session.user
    if is_system_manager(user) or is_suite_admin(user):
        return ""

    accounts = get_user_jmap_accounts(user)
    if not accounts:
        return "1=0"

    return f"""`tabMail Classification Rule`.account in ({", ".join(frappe.db.escape(account) for account in accounts)})"""


def has_permission(doc: Document, ptype: str, user: str | None = None) -> bool:
    if doc.doctype != "Mail Classification Rule":
        return False

    user = user or frappe.session.user

    if is_system_manager(user) or is_suite_admin(user):
        return True

    return doc.account in get_user_jmap_accounts(user)


def on_doctype_update() -> None:
    frappe.db.add_unique(
        "Mail Classification Rule",
        ["account", "sender", "is_default"],
        constraint_name="unique_classification_rule_sender",
    )
