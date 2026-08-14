# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""How the push-sync path reacts when the JMAP server cannot answer an ``Email/changes`` call:
method-level errors surface as exceptions instead of flowing downstream as fake changes results,
and the stored sync state is never advanced on failure."""

import unittest
from unittest import mock

from suite.mail.doctype.mail_message import mail_message
from suite.mail.jmap.services.mail.email import EmailService

FORBIDDEN = {"type": "forbidden", "description": "You are not authorized to perform this action"}


class EmailServiceChanges(unittest.TestCase):
    """``EmailService.changes`` — unwrap real results, raise on method-level errors."""

    def _service(self, body: dict) -> EmailService:
        service = EmailService("f7", mock.MagicMock())
        service._call_one = mock.MagicMock(return_value=body)
        return service

    def test_returns_method_response_body(self):
        body = {"created": [], "updated": ["e1"], "destroyed": [], "newState": "s2", "hasMoreChanges": False}
        service = self._service(body)

        self.assertEqual(service.changes("s1"), body)

    def test_raises_on_method_level_error(self):
        service = self._service({"error": FORBIDDEN})

        with self.assertRaises(RuntimeError) as ctx:
            service.changes("s1")

        self.assertIn("Email/changes failed", str(ctx.exception))
        self.assertIn("forbidden", str(ctx.exception))


class FetchChanges(unittest.TestCase):
    """``fetch_changes`` — server failures are logged and leave the sync state untouched."""

    def _run(self, changes: mock.Mock) -> tuple[mock.Mock, mock.Mock]:
        with (
            mock.patch.object(mail_message, "get_sync_state", return_value="s1"),
            mock.patch.object(mail_message, "update_sync_state") as update_sync_state,
            mock.patch.object(mail_message, "get_jmap_connection"),
            mock.patch.object(mail_message, "MailboxService"),
            mock.patch.object(mail_message, "EmailService") as email_service,
            mock.patch.object(mail_message, "log_mail_error") as log_mail_error,
        ):
            email_service.return_value.changes = changes
            mail_message.fetch_changes("user@example.test", "f7", email_state="s2")

        return update_sync_state, log_mail_error

    def test_method_level_error_is_logged_and_preserves_state(self):
        changes = mock.MagicMock(side_effect=RuntimeError(f"Email/changes failed: {FORBIDDEN}"))

        update_sync_state, log_mail_error = self._run(changes)

        log_mail_error.assert_called_once()
        update_sync_state.assert_not_called()

    def test_empty_response_is_not_an_error_and_preserves_state(self):
        update_sync_state, log_mail_error = self._run(mock.MagicMock(return_value={}))

        log_mail_error.assert_not_called()
        update_sync_state.assert_not_called()
