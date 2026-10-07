# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Sender rules: mail from a sender somebody has a rule for is given that rule's category, before
anything is read off the message itself.

A rule is a Mail Classification Rule, and comes from one of three places:

- an account, where a user corrected the category of a sender's mail and asked for it to stick;
- the site, where an admin wrote it for every account;
- the app, whose defaults (`default_rules.json`) are synced to each site when it migrates.
"""

import json
from collections.abc import Iterable
from pathlib import Path

import frappe

from suite.mail.classification.category import Category

DEFAULT_RULES_FILE = Path(__file__).with_name("default_rules.json")


class SenderRules:
    """The sender rules in force for one account.

    The account's own rules win over the site's, and the site's over the app's defaults. Within
    one of those, a rule for an address wins over one for its domain, and a rule for a domain over
    one for a domain above it.
    """

    def __init__(self, rules: Iterable[dict]) -> None:
        # One lookup per place a rule comes from, strongest first.
        self._scopes: tuple[dict[str, Category], ...] = ({}, {}, {})
        for rule in rules:
            self._scopes[_scope(rule)][rule["sender"].lower()] = Category(rule["category"].lower())

    @classmethod
    def for_account(cls, account: str) -> SenderRules:
        """The enabled rules of `account`, of the site and of the app."""

        return cls(
            frappe.get_all(
                "Mail Classification Rule",
                filters={"enabled": 1},
                or_filters=[["account", "=", account], ["account", "is", "not set"]],
                fields=["sender", "category", "account", "is_default"],
            )
        )

    def category_for(self, email: dict) -> Category | None:
        """The category the rules give `email` (an Email in JMAP wire form), or None when there is
        no rule for its sender."""

        senders = _senders(((email.get("from") or [{}])[0].get("email") or "").lower())
        for rules in self._scopes:
            for sender in senders:
                if category := rules.get(sender):
                    return category

        return None


def load_default_rules() -> list[dict]:
    """The rules shipped with the app: `{"sender": ..., "category": ...}` each."""

    return json.loads(DEFAULT_RULES_FILE.read_text())


def _scope(rule: dict) -> int:
    if rule.get("account"):
        return 0

    return 2 if rule.get("is_default") else 1


def _senders(address: str) -> list[str]:
    """What a rule for `address` can be written as, most specific first: the address itself, then
    "@" and each domain it is under ("@news.shop.example", "@shop.example", "@example")."""

    _local, at, domain = address.rpartition("@")
    if not at:
        return []

    labels = domain.split(".")
    return [address, *("@" + ".".join(labels[i:]) for i in range(len(labels)))]
