from typing import ClassVar

from suite.mail.jmap.services.calendars.calendars import CalendarsService


class CalendarEventNotificationService(CalendarsService):
    """Service for handling calendar event notification-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "CalendarEventNotification"
    EVENT_NOTIFICATION_PROPERTIES: ClassVar[list[str]] = [
        "id",
        "created",
        "changedBy",
        "comment",
        "type",
        "calendarEventId",
        "isDraft",
        "event",
        "eventPatch",
    ]

    def get(self, ids: list[str] | None = None, properties: list[str] | None = None) -> list[dict]:
        """Public method to get calendar event notifications, handling batching if a list of ids is provided."""

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(
                    self._get(batch, properties=properties or self.EVENT_NOTIFICATION_PROPERTIES).get(
                        "list", []
                    )
                )
        else:
            results.extend(
                self._get(properties=properties or self.EVENT_NOTIFICATION_PROPERTIES).get("list", [])
            )

        return results

    def delete(self, ids: list[str]) -> dict:
        """Public method to delete calendar event notifications, handling batching if the number of ids exceeds the server's maximum allowed in a single 'set' call."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in self.create_batches(ids, self.max_objects_in_set):
            body = self._delete(batch)

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result

    def query(
        self, filter: dict | None = None, position: int = 0, limit: int = 50, sort: list[dict] | None = None
    ) -> dict:
        """Public method to query calendar event notifications, handling batching if the number of results exceeds the server's maximum allowed in a single 'query' call."""

        ids = []
        total = None
        batch_size = min(limit, self.max_objects_in_get)
        sort = sort or [{"property": "created", "isAscending": True}]

        while len(ids) < limit:
            current_batch_size = min(batch_size, limit - len(ids))

            query_response = self._query(filter, position, current_batch_size, sort, calculate_total=total is None)

            batch_ids = query_response.get("ids", [])
            ids.extend(batch_ids)

            if total is None:
                total = query_response.get("total")

            if len(batch_ids) < current_batch_size or (total is not None and len(ids) >= total):
                break

            position += len(batch_ids)

        return {"ids": ids[:limit], "total": total}

    def changes(self, since_state: str) -> dict:
        """Public method to get changes to calendar event notifications since a given state."""

        return self._changes(since_state)
