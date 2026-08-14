# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""``CalendarEventNotification/get`` must always name the properties it wants.

Stalwart returns a reduced default property set when a ``get`` omits ``properties``, which
silently drops ``event``/``eventPatch`` from every notification. ``get_event_notifications``
therefore sends ``EVENT_NOTIFICATION_PROPERTIES`` unless the caller names its own set - on every
request, batched or not. These tests pin that forwarding, since a regression is invisible in the
response shape: the call still succeeds, the fields just stop arriving.
"""

import unittest
from unittest import mock

from jmap.core.limits import Limits

from suite.mail.jmap.calendars import EVENT_NOTIFICATION_PROPERTIES, get_event_notifications
from suite.mail.jmap.context import JMAPContext


class _StubCapabilities:
    """The slice of the resolved capabilities the context reads: server limits."""

    def __init__(self, max_objects_in_get: int) -> None:
        self.limits = Limits(max_objects_in_get=max_objects_in_get)


class _StubConnection:
    """The slice of ``JMAPConnection`` the context reads: server limits, via resolved capabilities."""

    def __init__(self, max_objects_in_get: int = 500) -> None:
        self._caps = _StubCapabilities(max_objects_in_get)

    def capabilities_for(self, account_id: str | None) -> _StubCapabilities:
        return self._caps


def _response(*ids: str) -> dict:
    """A ``CalendarEventNotification/get`` response body carrying the given notification ids."""

    return {"list": [{"id": i} for i in ids]}


def _get_call(ids: list[str] | None, properties: list[str]):
    """The expected ``ctx.call`` invocation for one ``CalendarEventNotification/get``."""

    return mock.call("CalendarEventNotification/get", {"ids": ids, "properties": properties})


class CalendarEventNotificationGetProperties(unittest.TestCase):
    """``get_event_notifications`` - what lands in the ``properties`` argument of each get call."""

    def _context(self, max_objects_in_get: int = 500) -> JMAPContext:
        ctx = JMAPContext(_StubConnection(max_objects_in_get), "account-1")
        ctx.call = mock.MagicMock(return_value=_response("n1"))
        return ctx

    def test_unbatched_get_sends_the_default_properties(self):
        ctx = self._context()
        get_event_notifications(ctx)

        ctx.call.assert_called_once_with(
            "CalendarEventNotification/get", {"ids": None, "properties": EVENT_NOTIFICATION_PROPERTIES}
        )

    def test_unbatched_get_sends_caller_supplied_properties(self):
        ctx = self._context()
        get_event_notifications(ctx, properties=["id", "created"])

        ctx.call.assert_called_once_with(
            "CalendarEventNotification/get", {"ids": None, "properties": ["id", "created"]}
        )

    def test_batched_get_sends_the_default_properties(self):
        ctx = self._context()
        get_event_notifications(ctx, ["n1"])

        ctx.call.assert_called_once_with(
            "CalendarEventNotification/get", {"ids": ["n1"], "properties": EVENT_NOTIFICATION_PROPERTIES}
        )

    def test_batched_get_sends_caller_supplied_properties(self):
        ctx = self._context()
        get_event_notifications(ctx, ["n1"], properties=["id", "type"])

        ctx.call.assert_called_once_with(
            "CalendarEventNotification/get", {"ids": ["n1"], "properties": ["id", "type"]}
        )

    def test_every_batch_carries_the_properties(self):
        """Not just the first one: forwarding inside the loop is the part that can rot."""

        ctx = self._context(max_objects_in_get=2)
        ctx.call.return_value = _response()
        get_event_notifications(ctx, ["n1", "n2", "n3", "n4", "n5"], properties=["id", "event"])

        self.assertEqual(
            ctx.call.call_args_list,
            [
                _get_call(["n1", "n2"], ["id", "event"]),
                _get_call(["n3", "n4"], ["id", "event"]),
                _get_call(["n5"], ["id", "event"]),
            ],
        )

    def test_every_batch_carries_the_defaults(self):
        ctx = self._context(max_objects_in_get=2)
        ctx.call.return_value = _response()
        get_event_notifications(ctx, ["n1", "n2", "n3"])

        self.assertEqual(
            [call.args[1]["properties"] for call in ctx.call.call_args_list],
            [EVENT_NOTIFICATION_PROPERTIES] * 2,
        )

    def test_empty_properties_falls_back_to_the_defaults(self):
        """``[]`` means "no preference", not "no properties" - an empty set would fetch nothing usable."""

        ctx = self._context()
        get_event_notifications(ctx, ["n1"], properties=[])
        get_event_notifications(ctx, properties=[])

        self.assertEqual(
            [call.args[1]["properties"] for call in ctx.call.call_args_list],
            [EVENT_NOTIFICATION_PROPERTIES] * 2,
        )

    def test_results_are_collected_across_batches(self):
        ctx = self._context(max_objects_in_get=2)
        ctx.call.side_effect = [_response("n1", "n2"), _response("n3")]
        results = get_event_notifications(ctx, ["n1", "n2", "n3"])

        self.assertEqual([r["id"] for r in results], ["n1", "n2", "n3"])

    def test_empty_body_yields_no_results(self):
        ctx = self._context()
        ctx.call.return_value = {}
        self.assertEqual(get_event_notifications(ctx, ["n1"]), [])
        self.assertEqual(get_event_notifications(ctx), [])


class CalendarEventNotificationDefaultProperties(unittest.TestCase):
    """The default set has to cover everything the Event Notification doctype renders."""

    def test_defaults_cover_every_field_the_formatter_reads(self):
        from suite.calendar.doctype.event_notification.event_notification import (
            format_event_notification,
        )

        notification = {
            "id": "n1",
            "created": "2026-08-11T09:00:00Z",
            "changedBy": {
                "name": "Jamie",
                "email": "jamie@example.test",
                "principalId": "p1",
                "scheduleId": "s1",
            },
            "comment": "moved a day",
            "type": "updated",
            "calendarEventId": "e1",
            "isDraft": False,
            "event": {"title": "Standup"},
            "eventPatch": {"start": "2026-08-12T09:00:00"},
        }
        self.assertEqual(sorted(notification), sorted(EVENT_NOTIFICATION_PROPERTIES))

        formatted = format_event_notification("account-1", notification)
        self.assertEqual(formatted["changed_by_name"], "Jamie")
        self.assertEqual(formatted["calendar_event"], "account-1|e1")
        self.assertIn("Standup", formatted["event"])
        self.assertIn("2026-08-12T09:00:00", formatted["event_patch"])
