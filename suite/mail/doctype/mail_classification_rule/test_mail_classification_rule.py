# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from suite.mail.classification import Category, SenderRules
from suite.mail.doctype.mail_classification_rule import mail_classification_rule as module
from suite.mail.doctype.mail_classification_rule.mail_classification_rule import (
    remember_senders,
    sync_default_rules,
)

ACCOUNT = "test-classification-rules"
OTHER_ACCOUNT = "test-classification-rules-other"
USER = "classification-rules@example.test"


def _email(sender: str) -> dict:
    return {"from": [{"name": None, "email": sender}]}


class IntegrationTestMailClassificationRule(IntegrationTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        for account in (ACCOUNT, OTHER_ACCOUNT):
            # Straight into the table: inserting an account the usual way reaches for its mail server.
            frappe.get_doc(
                {
                    "doctype": "JMAP Account",
                    "name": account,
                    "account_id": account,
                    "_name": f"{account}@x.test",
                }
            ).db_insert()

        if not frappe.db.exists("User", USER):
            user = frappe.get_doc(
                {"doctype": "User", "email": USER, "first_name": "Rules", "send_welcome_email": 0}
            )
            user.insert(ignore_permissions=True)
            user.add_roles("Suite User")

    def setUp(self) -> None:
        super().setUp()
        # The test case rolls back once per class, and a rule one test wrote must not decide the next.
        frappe.db.savepoint("classification_rule_test")

    def tearDown(self) -> None:
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point="classification_rule_test")
        super().tearDown()

    def rule(self, sender: str, category: str, account: str | None = None) -> module.MailClassificationRule:
        return frappe.get_doc(
            {
                "doctype": "Mail Classification Rule",
                "sender": sender,
                "category": category,
                "account": account,
            }
        ).insert()

    def category(self, sender: str, account: str = ACCOUNT) -> Category | None:
        return SenderRules.for_account(account).category_for(_email(sender))

    def as_user(self, accounts: list[str]) -> mock._patch:
        """Acts as a mail user who has access to `accounts`."""

        frappe.set_user(USER)
        return mock.patch.object(module, "get_user_jmap_accounts", return_value=accounts)


class TestRulesInForce(IntegrationTestMailClassificationRule):
    """Which rules an account's mail is classified by."""

    def test_an_account_is_classified_by_its_own_rules_and_the_sites(self):
        self.rule("hello@shop.example", "Promotions", ACCOUNT)
        self.rule("@crm.example", "Updates")

        self.assertEqual(self.category("hello@shop.example"), Category.PROMOTIONS)
        self.assertEqual(self.category("notify@crm.example"), Category.UPDATES)

    def test_an_account_is_not_classified_by_another_accounts_rules(self):
        self.rule("hello@shop.example", "Promotions", OTHER_ACCOUNT)

        self.assertIsNone(self.category("hello@shop.example"))

    def test_a_disabled_rule_is_ignored(self):
        rule = self.rule("hello@shop.example", "Promotions", ACCOUNT)
        rule.enabled = 0
        rule.save()

        self.assertIsNone(self.category("hello@shop.example"))

    def test_a_sender_is_stored_normalised(self):
        rule = self.rule("  @Shop.Example ", "Promotions", ACCOUNT)

        self.assertEqual(rule.sender, "@shop.example")

    def test_a_sender_that_is_neither_an_address_nor_a_domain_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.rule("shop.example", "Promotions", ACCOUNT)

    def test_a_sender_has_one_rule_per_account(self):
        self.rule("hello@shop.example", "Promotions", ACCOUNT)

        with self.assertRaises(frappe.ValidationError):
            self.rule("hello@shop.example", "Updates", ACCOUNT)

        # Another account, and the site, may each have their own say about the same sender.
        self.rule("hello@shop.example", "Updates", OTHER_ACCOUNT)
        self.rule("hello@shop.example", "Social")


class TestWhoMayWriteRules(IntegrationTestMailClassificationRule):
    """A user writes rules for the accounts they can use; rules for everyone are an admin's."""

    def test_a_user_can_write_a_rule_for_their_own_account(self):
        with self.as_user([ACCOUNT]):
            rule = self.rule("hello@shop.example", "Promotions", ACCOUNT)

            rule.category = "Updates"
            rule.save()

    def test_a_user_cannot_write_a_rule_for_an_account_that_is_not_theirs(self):
        with self.as_user([ACCOUNT]), self.assertRaises(frappe.PermissionError):
            self.rule("hello@shop.example", "Promotions", OTHER_ACCOUNT)

    def test_a_user_cannot_change_another_accounts_rule(self):
        rule = self.rule("hello@shop.example", "Promotions", OTHER_ACCOUNT)

        with self.as_user([ACCOUNT]), self.assertRaises(frappe.PermissionError):
            frappe.get_doc("Mail Classification Rule", rule.name).save()

    def test_a_user_cannot_write_a_site_wide_rule(self):
        with self.as_user([ACCOUNT]), self.assertRaises(frappe.PermissionError):
            self.rule("@crm.example", "Updates")

    def test_a_user_is_listed_only_the_rules_of_their_accounts(self):
        mine = self.rule("hello@shop.example", "Promotions", ACCOUNT)
        self.rule("hello@shop.example", "Updates", OTHER_ACCOUNT)
        self.rule("@crm.example", "Updates")

        with self.as_user([ACCOUNT]):
            listed = frappe.get_list("Mail Classification Rule", pluck="name")

        self.assertEqual(listed, [mine.name])


class TestRememberedSenders(IntegrationTestMailClassificationRule):
    """`remember_senders` - what a correction leaves behind for the mail still to come."""

    def test_future_mail_from_a_remembered_sender_is_given_the_category(self):
        with self.as_user([ACCOUNT]):
            remember_senders(ACCOUNT, ["Hello@Shop.Example"], Category.UPDATES)

        self.assertEqual(self.category("hello@shop.example"), Category.UPDATES)
        self.assertIsNone(self.category("hello@shop.example", OTHER_ACCOUNT))

    def test_remembering_a_sender_again_changes_its_rule(self):
        with self.as_user([ACCOUNT]):
            remember_senders(ACCOUNT, ["hello@shop.example"], Category.UPDATES)
            remember_senders(ACCOUNT, ["hello@shop.example"], Category.PROMOTIONS)

        self.assertEqual(self.category("hello@shop.example"), Category.PROMOTIONS)
        self.assertEqual(frappe.db.count("Mail Classification Rule", {"account": ACCOUNT}), 1)

    def test_remembering_a_sender_switches_a_disabled_rule_back_on(self):
        rule = self.rule("hello@shop.example", "Promotions", ACCOUNT)
        rule.enabled = 0
        rule.save()

        remember_senders(ACCOUNT, ["hello@shop.example"], Category.UPDATES)

        self.assertEqual(self.category("hello@shop.example"), Category.UPDATES)

    def test_a_sender_cannot_be_remembered_for_an_account_that_is_not_the_users(self):
        with self.as_user([ACCOUNT]), self.assertRaises(frappe.PermissionError):
            remember_senders(OTHER_ACCOUNT, ["hello@shop.example"], Category.UPDATES)


class TestDefaultRules(IntegrationTestMailClassificationRule):
    """The rules the app ships, synced to the site when it migrates."""

    def sync(self, *shipped: tuple[str, str]) -> None:
        rules = [{"sender": sender, "category": category} for sender, category in shipped]
        with mock.patch.object(module, "load_default_rules", return_value=rules):
            sync_default_rules()

    def default(self, sender: str) -> module.MailClassificationRule:
        return frappe.get_doc("Mail Classification Rule", {"sender": sender, "is_default": 1})

    def test_the_rules_the_app_ships_are_in_force_once_synced(self):
        self.sync(("@network.example", "Social"))

        self.assertEqual(self.category("hi@network.example"), Category.SOCIAL)

    def test_a_rule_the_app_changed_or_dropped_follows_it(self):
        self.sync(("@network.example", "Social"), ("@gone.example", "Updates"))

        self.sync(("@network.example", "Forums"))

        self.assertEqual(self.category("hi@network.example"), Category.FORUMS)
        self.assertIsNone(self.category("hi@gone.example"))

    def test_a_default_the_site_disabled_stays_disabled(self):
        self.sync(("@network.example", "Social"))
        rule = self.default("@network.example")
        rule.enabled = 0
        rule.save()

        self.sync(("@network.example", "Social"), ("@other.example", "Updates"))

        self.assertIsNone(self.category("hi@network.example"))

    def test_the_sites_own_rules_are_left_alone(self):
        self.rule("@crm.example", "Updates")
        self.rule("hello@shop.example", "Promotions", ACCOUNT)

        self.sync(("@network.example", "Social"))
        self.sync()

        self.assertEqual(self.category("notify@crm.example"), Category.UPDATES)
        self.assertEqual(self.category("hello@shop.example"), Category.PROMOTIONS)

    def test_a_default_can_be_neither_edited_nor_deleted_by_hand(self):
        self.sync(("@network.example", "Social"))
        rule = self.default("@network.example")

        rule.category = "Updates"
        with self.assertRaises(frappe.ValidationError):
            rule.save()

        with self.assertRaises(frappe.ValidationError):
            self.default("@network.example").delete()

    def test_a_default_cannot_be_written_by_hand(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(
                {
                    "doctype": "Mail Classification Rule",
                    "sender": "@network.example",
                    "category": "Social",
                    "is_default": 1,
                }
            ).insert()

    def test_a_sites_rule_wins_over_the_default_for_the_same_sender(self):
        self.sync(("@network.example", "Social"))
        self.rule("@network.example", "Updates")

        self.assertEqual(self.category("hi@network.example"), Category.UPDATES)
