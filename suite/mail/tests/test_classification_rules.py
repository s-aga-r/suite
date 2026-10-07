# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Sender rules, and the correction that writes them: what a user or an admin has said about a
sender decides the category of its mail, ahead of anything read off the message."""

import unittest
from unittest import mock

from suite.mail.api import mail as mail_api
from suite.mail.classification import Category, SenderRules, classify
from suite.mail.classification.rules import load_default_rules
from suite.mail.tests.test_email_classification import ACCOUNT, NEWSLETTER, _email, _keywords, _Mail

MAILING_LIST = {"List-Id": "<dev.lists.example.org>", "List-Post": "<mailto:dev@lists.example.org>"}


def _rule(sender: str, category: str, account: str | None = None, is_default: int = 0) -> dict:
    return {"sender": sender, "category": category, "account": account, "is_default": is_default}


class Rules(unittest.TestCase):
    """`SenderRules` - which rule, of those in force for an account, decides a sender's mail."""

    def category(self, sender: str, *rules: dict) -> Category | None:
        return SenderRules(rules).category_for(_email(sender=sender))

    def test_a_rule_for_an_address_covers_that_address_only(self):
        rule = _rule("alice@shop.example", "Updates")

        self.assertEqual(self.category("alice@shop.example", rule), Category.UPDATES)
        self.assertIsNone(self.category("bob@shop.example", rule))

    def test_a_rule_for_a_domain_covers_its_senders_and_subdomains(self):
        rule = _rule("@shop.example", "Promotions")

        self.assertEqual(self.category("hello@shop.example", rule), Category.PROMOTIONS)
        self.assertEqual(self.category("hello@news.shop.example", rule), Category.PROMOTIONS)

    def test_a_rule_for_a_domain_does_not_cover_one_that_merely_ends_like_it(self):
        self.assertIsNone(self.category("bob@notshop.example", _rule("@shop.example", "Promotions")))

    def test_a_sender_is_matched_whatever_its_case(self):
        rule = _rule("alice@shop.example", "Updates")

        self.assertEqual(self.category("Alice@Shop.Example", rule), Category.UPDATES)

    def test_a_rule_for_an_address_wins_over_one_for_its_domain(self):
        rules = (_rule("@shop.example", "Promotions"), _rule("billing@shop.example", "Updates"))

        self.assertEqual(self.category("billing@shop.example", *rules), Category.UPDATES)
        self.assertEqual(self.category("hello@shop.example", *rules), Category.PROMOTIONS)

    def test_a_rule_for_a_subdomain_wins_over_one_for_the_domain_above(self):
        rules = (_rule("@shop.example", "Promotions"), _rule("@orders.shop.example", "Updates"))

        self.assertEqual(self.category("no-reply@orders.shop.example", *rules), Category.UPDATES)

    def test_an_accounts_rule_wins_over_the_sites_and_the_sites_over_a_default(self):
        default = _rule("@network.example", "Social", is_default=1)
        site = _rule("@network.example", "Updates")
        account = _rule("@network.example", "Primary", account=ACCOUNT)

        self.assertEqual(self.category("hi@network.example", default), Category.SOCIAL)
        self.assertEqual(self.category("hi@network.example", default, site), Category.UPDATES)
        self.assertEqual(self.category("hi@network.example", site, default, account), Category.PRIMARY)

    def test_mail_without_a_sender_has_no_rule(self):
        email = _email()
        email["from"] = None

        self.assertIsNone(SenderRules([_rule("@example.org", "Updates")]).category_for(email))


class DefaultRules(unittest.TestCase):
    """The rules the app ships, which every site starts with."""

    def category(self, sender: str, headers: dict[str, str] | None = None) -> Category:
        return classify(_email(sender=sender, headers=headers), SenderRules(load_default_rules()))

    def test_mail_from_a_social_network_is_social(self):
        self.assertEqual(self.category("messages-noreply@linkedin.com", NEWSLETTER), Category.SOCIAL)
        self.assertEqual(self.category("notification@facebookmail.com"), Category.SOCIAL)
        # Networks send from subdomains as well.
        self.assertEqual(self.category("pinbot@explore.pinterest.com", NEWSLETTER), Category.SOCIAL)

    def test_a_domain_that_merely_ends_like_a_social_network_is_not_social(self):
        self.assertEqual(self.category("bob@notlinkedin.com"), Category.PRIMARY)

    def test_every_default_rule_names_a_sender_and_a_category(self):
        for rule in load_default_rules():
            with self.subTest(rule=rule):
                self.assertRegex(rule["sender"], r"^(@|[^@\s]+@)[a-z0-9-]+(\.[a-z0-9-]+)+$")
                self.assertIn(rule["category"].lower(), [category.value for category in Category])


class RulesOutrankTheMessage(unittest.TestCase):
    """A rule is somebody's say-so about a sender, and the headers only a reading of one message."""

    def test_a_rule_decides_mail_the_headers_would_have_placed_elsewhere(self):
        rules = SenderRules([_rule("bob@example.net", "Primary", account=ACCOUNT)])

        self.assertEqual(classify(_email(sender="bob@example.net", headers=MAILING_LIST)), Category.FORUMS)
        self.assertEqual(
            classify(_email(sender="bob@example.net", headers=MAILING_LIST), rules), Category.PRIMARY
        )

    def test_mail_no_rule_is_for_is_left_to_the_headers(self):
        rules = SenderRules([_rule("bob@example.net", "Primary", account=ACCOUNT)])

        self.assertEqual(
            classify(_email(sender="carol@example.net", headers=MAILING_LIST), rules), Category.FORUMS
        )

    def test_fetched_mail_is_given_the_category_of_its_senders_rule(self):
        mail = _Mail(_email("e1", sender="hello@shop.example", headers=NEWSLETTER))
        mail.rules = [_rule("hello@shop.example", "Updates", account=ACCOUNT)]

        [message] = mail.fetch()

        self.assertEqual(
            [call["update"] for call in mail.calls("Email/set")],
            [{"e1": {"keywords/category_updates": True}}],
        )
        self.assertEqual(_keywords(message), {"category_updates": True})


class Correction(unittest.TestCase):
    """A user moves mail to the category it should have been given."""

    def move(self, mail: _Mail, category: str, *ids: str, remember_senders: bool = False) -> mock.Mock:
        """Corrects `ids` to `category`; returns the stand-in the senders to remember are handed to."""

        with mail.patched(), mock.patch.object(mail_api, "remember_senders_category") as remember:
            self.moved = mail_api.set_mails_category(
                ACCOUNT, list(ids or mail.emails), category, remember_senders=remember_senders
            )

        return remember

    def classified(self, *emails: dict) -> _Mail:
        """A mailbox whose `emails` have been fetched, and so classified, already."""

        mail = _Mail(*emails)
        mail.fetch()
        mail.server.requests.clear()
        return mail

    def test_the_message_takes_the_new_category_and_gives_up_the_old(self):
        mail = self.classified(_email("e1", sender="hello@shop.example", headers=NEWSLETTER))

        self.move(mail, "updates")

        self.assertEqual(mail.emails["e1"]["keywords"].get("category_updates"), True)
        self.assertFalse(mail.emails["e1"]["keywords"].get("category_promotions"))

    def test_the_cached_message_carries_only_the_new_category(self):
        mail = self.classified(
            _email("e1", sender="hello@shop.example", headers=NEWSLETTER, keywords={"$seen": True})
        )

        self.move(mail, "updates")

        self.assertEqual(_keywords(mail.cache["e1"]), {"$seen": True, "category_updates": True})

    def test_mail_that_carries_no_category_is_left_as_it_is(self):
        # A whole thread is handed over, the user's own replies in it included.
        mail = self.classified(
            _email("e1", sender="hello@shop.example", headers=NEWSLETTER),
            _email("e2", sender="user@example.test", mailbox="sent"),
        )

        self.move(mail, "updates")

        self.assertEqual(self.moved, ["e1"])
        self.assertEqual(mail.emails["e2"]["keywords"], {})

    def test_the_senders_of_the_moved_mail_are_remembered_when_asked(self):
        mail = self.classified(
            _email("e1", sender="hello@shop.example", headers=NEWSLETTER),
            _email("e2", sender="user@example.test", mailbox="sent"),
        )

        remember = self.move(mail, "updates", remember_senders=True)

        remember.assert_called_once_with(ACCOUNT, ["hello@shop.example"], Category.UPDATES)

    def test_no_sender_is_remembered_unless_asked(self):
        mail = self.classified(_email("e1", sender="hello@shop.example", headers=NEWSLETTER))

        remember = self.move(mail, "updates")

        remember.assert_not_called()

    def test_a_mail_is_served_with_the_category_it_was_moved_to(self):
        # What the message's menu reads, to leave out the category it is in already.
        mail = self.classified(_email("e1", sender="hello@shop.example", headers=NEWSLETTER))
        self.assertEqual(mail_api.serialize_mail(mail.cache["e1"])["category"], "promotions")

        self.move(mail, "updates")

        self.assertEqual(mail_api.serialize_mail(mail.cache["e1"])["category"], "updates")

    def test_a_mail_without_a_category_is_served_without_one(self):
        mail = self.classified(_email("e1", sender="user@example.test", mailbox="sent"))

        self.assertIsNone(mail_api.serialize_mail(mail.cache["e1"])["category"])

    def test_a_category_that_does_not_exist_is_refused(self):
        mail = self.classified(_email("e1", sender="hello@shop.example", headers=NEWSLETTER))

        with self.assertRaises(Exception):
            self.move(mail, "spam")

        self.assertEqual(mail.calls("Email/set"), [])
