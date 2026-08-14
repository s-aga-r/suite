from typing import ClassVar

from suite.mail.jmap.services.calendars.calendars import CalendarsService


class CalendarService(CalendarsService):
    """Service for handling calendar-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "Calendar"

    def create(self, calendars: list[dict]) -> dict:
        """Public method to create calendars, handling batching if the number of calendars exceeds the server's maximum allowed in a single 'set' call."""

        result = {"created": {}, "notCreated": {}}
        for batch in self.create_batches(calendars, self.max_objects_in_set):
            payload = {}
            kwargs = {}
            for calendar in batch:
                payload[calendar["creation_id"]] = {
                    "name": calendar["name"],
                    "color": calendar.get("color"),
                    "description": calendar.get("description"),
                    "sortOrder": int(calendar.get("sort_order") or 0),
                    "timeZone": calendar.get("time_zone"),
                    "isSubscribed": bool(calendar.get("is_subscribed") or False),
                    "isVisible": bool(calendar.get("is_visible", True)),
                    "includeInAvailability": calendar.get("include_in_availability") or "all",
                }

                if bool(calendar.get("is_default") or False):
                    kwargs["onSuccessSetIsDefault"] = f"#{calendar['creation_id']}"

            body = self._create(payload, **kwargs)

            result["created"].update(body.get("created", {}))
            if not_created := body.get("notCreated", {}):
                result["notCreated"].update(not_created)

        return result

    def get(self, ids: list[str] | None = None) -> list[dict]:
        """Public method to get calendars, handling batching if a list of ids is provided."""

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(self._get(batch).get("list", []))
        else:
            results.extend(self._get().get("list", []))

        return results

    def update(self, calendars: list[dict]) -> dict:
        """Public method to update calendars, handling batching if the number of calendars exceeds the server's maximum allowed in a single 'set' call."""

        result = {"updated": [], "notUpdated": {}}
        for batch in self.create_batches(calendars, self.max_objects_in_set):
            payload = {}
            kwargs = {}
            for calendar in batch:
                payload[calendar["id"]] = {
                    "name": calendar["name"],
                    "color": calendar.get("color"),
                    "description": calendar.get("description"),
                    "sortOrder": int(calendar.get("sort_order") or 0),
                    "timeZone": calendar.get("time_zone"),
                    "isSubscribed": bool(calendar.get("is_subscribed") or False),
                    "isVisible": bool(calendar.get("is_visible", True)),
                    "includeInAvailability": calendar.get("include_in_availability") or "all",
                }

                if bool(calendar.get("is_default") or False):
                    kwargs["onSuccessSetIsDefault"] = calendar["id"]

            body = self._update(payload, **kwargs)

            result["updated"].extend(body.get("updated", {}).keys())
            if not_updated := body.get("notUpdated", {}):
                result["notUpdated"].update(not_updated)

        return result

    def delete(self, ids: list[str], remove_events: bool = False) -> dict:
        """Public method to delete calendars, handling batching if the number of calendar ids exceeds the server's maximum allowed in a single 'set' call."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in self.create_batches(ids, self.max_objects_in_set):
            body = self._delete(batch, onDestroyRemoveEvents=remove_events)

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result

    def changes(self, since_state: str) -> dict:
        """Public method to get calendar changes since a given state."""

        return self._changes(since_state)

    def get_default(self, raise_exception: bool = False) -> str | None:
        """Returns the ID of the default calendar, or None if no default calendar is found. If raise_exception is True, raises a ValueError if no default calendar is found."""

        for calendar in self.calendars:
            if calendar.get("isDefault"):
                return calendar["id"]

        if raise_exception:
            raise ValueError("No default calendar found.")
