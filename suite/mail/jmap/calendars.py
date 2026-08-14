"""Calendar-domain helpers on top of jmaplib: calendars, JSCalendar events, event notifications,
participant identities, and principals."""

from uuid import uuid7

from suite.mail.jmap.context import JMAPContext
from suite.mail.utils.dt import normalize_utc_z
from suite.utils.dt import utcnow

EVENT_NOTIFICATION_PROPERTIES = [
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


# -- calendars -------------------------------------------------------------- #
def _calendar_payload(calendar: dict) -> dict:
    return {
        "name": calendar["name"],
        "color": calendar.get("color"),
        "description": calendar.get("description"),
        "sortOrder": int(calendar.get("sort_order") or 0),
        "timeZone": calendar.get("time_zone"),
        "isSubscribed": bool(calendar.get("is_subscribed") or False),
        "isVisible": bool(calendar.get("is_visible", True)),
        "includeInAvailability": calendar.get("include_in_availability") or "all",
    }


def create_calendars(ctx: JMAPContext, calendars: list[dict]) -> dict:
    """Creates calendars from simplified dicts, honoring is_default via onSuccessSetIsDefault."""

    payload = {}
    kwargs = {}
    for calendar in calendars:
        payload[calendar["creation_id"]] = _calendar_payload(calendar)

        if bool(calendar.get("is_default") or False):
            kwargs["onSuccessSetIsDefault"] = f"#{calendar['creation_id']}"

    return ctx.create("Calendar", payload, **kwargs)


def update_calendars(ctx: JMAPContext, calendars: list[dict]) -> dict:
    """Updates calendars from simplified dicts, honoring is_default via onSuccessSetIsDefault."""

    payload = {}
    kwargs = {}
    for calendar in calendars:
        payload[calendar["id"]] = _calendar_payload(calendar)

        if bool(calendar.get("is_default") or False):
            kwargs["onSuccessSetIsDefault"] = calendar["id"]

    return ctx.update("Calendar", payload, **kwargs)


def delete_calendars(ctx: JMAPContext, ids: list[str], remove_events: bool = False) -> dict:
    """Deletes calendars by ids, optionally destroying the events they contain."""

    return ctx.destroy("Calendar", ids, onDestroyRemoveEvents=remove_events)


def get_default_calendar_id(ctx: JMAPContext, raise_exception: bool = False) -> str | None:
    """Returns the ID of the default calendar, or None if not found."""

    for calendar in ctx.calendars:
        if calendar.get("isDefault"):
            return calendar["id"]

    if raise_exception:
        raise ValueError("No default calendar found.")


# -- calendar events -------------------------------------------------------- #
def create_calendar_events(
    ctx: JMAPContext, events: list[dict], send_scheduling_messages: bool = False
) -> dict:
    """Creates calendar events from simplified dicts; sendSchedulingMessages controls whether the
    server sends invitations for the created events."""

    payload = {}
    for event in events:
        timestamp = utcnow()

        calendar_ids = event.get("calendar_ids")
        if not calendar_ids:
            calendar_ids = [get_default_calendar_id(ctx, raise_exception=True)]

        organizer = event.get("organizer")
        if not organizer:
            organizer = get_default_participant_identity(ctx, raise_exception=True)

        organizer = organizer.lower()
        if not organizer.startswith("mailto:"):
            organizer = f"mailto:{organizer}"

        payload[event["creation_id"]] = {
            "@type": "Event",
            "uid": event["uid"],
            "organizerCalendarAddress": organizer,
            "calendarIds": {id: True for id in calendar_ids},
            "status": event.get("status"),
            "isDraft": bool(event.get("is_draft") or False),
            "title": event.get("title"),
            "start": event.get("start"),
            "duration": event.get("duration"),
            "timeZone": event.get("time_zone"),
            "recurrenceRule": event.get("recurrence_rule") or None,
            "showWithoutTime": bool(event.get("show_without_time") or False),
            "privacy": event.get("privacy"),
            "freeBusyStatus": event.get("free_busy_status"),
            "description": event.get("description"),
            "locations": _get_locations_map(event.get("locations")),
            "links": _get_links_map(event.get("links")),
            "participants": _get_participants_map(event.get("participants")),
            "alerts": _get_alerts_map(event.get("alerts")),
            "useDefaultAlerts": bool(event.get("use_default_alerts") or False),
            "created": timestamp,
            "updated": timestamp,
        }

    return ctx.create("CalendarEvent", payload, sendSchedulingMessages=send_scheduling_messages)


def update_calendar_events(
    ctx: JMAPContext, events: list[dict], send_scheduling_messages: bool = False
) -> dict:
    """Updates calendar events from simplified dicts; sendSchedulingMessages controls whether the
    server notifies attendees of the update."""

    payload = {}
    for event in events:
        calendar_ids = event.get("calendar_ids")
        if not calendar_ids:
            calendar_ids = [get_default_calendar_id(ctx, raise_exception=True)]

        payload[event["id"]] = {
            "@type": "Event",
            "calendarIds": {id: True for id in calendar_ids},
            "privacy": event.get("privacy"),
            "freeBusyStatus": event.get("free_busy_status"),
            "alerts": _get_alerts_map(event.get("alerts")),
        }

        organizer = event.get("organizer")

        if organizer:
            organizer = organizer.lower()

            if not organizer.startswith("mailto:"):
                organizer = f"mailto:{organizer}"

        payload[event["id"]].update(
            {
                "uid": event["uid"],
                "organizerCalendarAddress": organizer,
                "status": event.get("status"),
                "isDraft": bool(event.get("is_draft") or False),
                "title": event.get("title"),
                "start": event.get("start"),
                "duration": event.get("duration"),
                "timeZone": event.get("time_zone"),
                "recurrenceRule": event.get("recurrence_rule") or None,
                "showWithoutTime": bool(event.get("show_without_time") or False),
                "description": event.get("description"),
                "locations": _get_locations_map(event.get("locations")),
                "links": _get_links_map(event.get("links")),
                "participants": _get_participants_map(event.get("participants")),
                "useDefaultAlerts": bool(event.get("use_default_alerts") or False),
                "updated": utcnow(),
            }
        )

        # The caller may force a SEQUENCE bump (iTIP) so attendee clients apply the update
        # instead of ignoring it as a duplicate. Only set it when provided; otherwise leave
        # the server-managed value alone.
        if (sequence := event.get("sequence")) is not None:
            payload[event["id"]]["sequence"] = int(sequence)

    return ctx.update("CalendarEvent", payload, sendSchedulingMessages=send_scheduling_messages)


def set_event_calendar_ids(ctx: JMAPContext, mapping: dict[str, dict[str, bool]]) -> dict:
    """Replaces the calendarIds of each given event with the provided map.

    Used by the import rollback flow to move events out of the staging calendar into their final
    calendar(s) once every event has been created; only calendarIds is touched (`updated` is
    server-managed for CalendarEvent)."""

    return ctx.update("CalendarEvent", {id: {"calendarIds": calendar_ids} for id, calendar_ids in mapping.items()})


def delete_calendar_events(ctx: JMAPContext, ids: list[str], send_scheduling_messages: bool = False) -> dict:
    """Deletes calendar events by ids. If send_scheduling_messages is True, the server sends
    cancellation scheduling messages for the destroyed events; pass False to suppress them
    (e.g. when the client sends its own cancellations)."""

    return ctx.destroy("CalendarEvent", ids, sendSchedulingMessages=send_scheduling_messages)


def query_calendar_events(
    ctx: JMAPContext,
    filter: dict | None = None,
    position: int = 0,
    limit: int = 50,
    sort: list[dict] | None = None,
    time_zone: str | None = None,
    expand_recurrences: bool = False,
) -> dict:
    """Queries calendar events, earliest first by default, returning `{"ids", "total"}`."""

    return ctx.query(
        "CalendarEvent",
        filter,
        position,
        limit,
        sort or [{"property": "start", "isAscending": True}],
        timeZone=time_zone,
        expandRecurrences=expand_recurrences,
    )


def parse_calendar_events(ctx: JMAPContext, blob_ids: list[str]) -> dict:
    """Parses calendar event blobs into JSCalendar events via 'CalendarEvent/parse'."""

    from suite.mail.jmap.context import create_batches

    result = {"parsed": {}, "notFound": {}, "notParsable": {}}
    for batch in create_batches(blob_ids, ctx.limits.max_objects_in_get):
        body = ctx.call("CalendarEvent/parse", {"blobIds": batch})

        result["parsed"].update(body.get("parsed", {}))
        # The server reports notFound/notParsable as blob-id arrays; keep the
        # dict shape callers read (.keys()) by keying the ids.
        if not_found := body.get("notFound"):
            result["notFound"].update(dict.fromkeys(not_found) if isinstance(not_found, list) else not_found)
        if not_parsable := body.get("notParsable"):
            result["notParsable"].update(
                dict.fromkeys(not_parsable) if isinstance(not_parsable, list) else not_parsable
            )

    return result


def get_base_event_ids(ctx: JMAPContext, ids: list[str]) -> dict[str, str]:
    """Maps event ids (including synthetic ids from recurrence-expanded queries) to the id of the
    real event they belong to.

    Resolved via a lightweight get requesting only baseEventId, which the server derives directly
    from the id itself — unlike a uid query, it does not depend on the search index (updated
    asynchronously), so it works immediately after an event is created."""

    return {
        event["id"]: event.get("baseEventId") or event["id"]
        for event in ctx.get_all("CalendarEvent", ids, properties=["id", "baseEventId"])
    }


def get_master_event_ids(ctx: JMAPContext, uids: list[str]) -> list[str]:
    """Gets master event IDs for a list of UIDs."""

    return query_calendar_events(
        ctx,
        {"operator": "OR", "conditions": [{"uid": uid} for uid in uids]},
        position=0,
        limit=len(uids),
        expand_recurrences=False,
    ).get("ids", [])


def update_event_instance(
    ctx: JMAPContext,
    id: str,
    recurrence_id: str,
    patch: dict,
    send_scheduling_messages: bool = False,
    sequence: int | None = None,
) -> dict:
    """Updates a specific instance of a recurring calendar event by applying the provided patch
    to the master event's recurrence overrides."""

    if not id or not recurrence_id:
        raise ValueError("Both 'id' and 'recurrence_id' are required.")
    if not patch:
        raise ValueError("Patch data is required to update an instance.")

    events = ctx.get_all("CalendarEvent", [id])
    if not events:
        raise ValueError(f"Event with id '{id}' not found.")

    event = events[0]
    recurrence_overrides = event.get("recurrenceOverrides", {}) or {}

    def _mailto(value: str) -> str:
        value = value.lower()
        return value if value.startswith("mailto:") else f"mailto:{value}"

    FIELD_MAP = {
        "calendar_ids": ("calendarIds", lambda v: {i: True for i in v}),
        "privacy": ("privacy", None),
        "free_busy_status": ("freeBusyStatus", None),
        "alerts": ("alerts", _get_alerts_map),
        "organizer": ("organizerCalendarAddress", _mailto),
        "uid": ("uid", None),
        "status": ("status", None),
        "title": ("title", None),
        "start": ("start", None),
        "duration": ("duration", None),
        "time_zone": ("timeZone", None),
        "recurrence_rule": ("recurrenceRule", None),
        "show_without_time": ("showWithoutTime", lambda v: bool(v)),
        "description": ("description", None),
        "locations": ("locations", _get_locations_map),
        "links": ("links", _get_links_map),
        "participants": ("participants", _get_participants_map),
        "use_default_alerts": ("useDefaultAlerts", lambda v: bool(v)),
    }

    out = {}
    for key, (target, transform) in FIELD_MAP.items():
        if key in patch:
            value = patch[key]
            out[target] = transform(value) if transform else value

    payload = {id: {}}

    if recurrence_id in recurrence_overrides:
        payload[id].update({f"recurrenceOverrides/{recurrence_id}/{k}": v for k, v in out.items()})
    else:
        recurrence_overrides[recurrence_id] = out
        payload = {id: {"recurrenceOverrides": recurrence_overrides}}

    payload[id]["updated"] = utcnow()

    # Bump the master SEQUENCE (iTIP) so attendees' clients accept the re-sent series.
    if sequence is not None:
        payload[id]["sequence"] = int(sequence)

    return ctx.update("CalendarEvent", payload, sendSchedulingMessages=send_scheduling_messages)


def set_participation_status(
    ctx: JMAPContext,
    id: str,
    participant_uid: str,
    participation_status: str,
    send_scheduling_messages: bool = False,
) -> dict:
    """Patches a single participant's participationStatus without rewriting the event.

    Used by the RSVP link endpoint, where a guest updates only their own response on the
    organizer's copy of the event.
    """

    if not id or not participant_uid:
        raise ValueError("Both 'id' and 'participant_uid' are required.")

    payload = {
        id: {
            f"participants/{participant_uid}/participationStatus": participation_status.lower(),
            "updated": utcnow(),
        }
    }

    return ctx.update("CalendarEvent", payload, sendSchedulingMessages=send_scheduling_messages)


def delete_event_instance(
    ctx: JMAPContext, id: str, recurrence_id: str, send_scheduling_messages: bool = False
) -> dict:
    """Deletes a specific instance of a recurring calendar event by marking it as excluded in the
    master event's recurrence overrides. If send_scheduling_messages is True, the server sends a
    cancellation for the excluded instance; pass False to suppress it."""

    if not id or not recurrence_id:
        raise ValueError("Both 'id' and 'recurrence_id' are required.")

    events = ctx.get_all("CalendarEvent", [id])
    if not events:
        raise ValueError(f"Event with id '{id}' not found.")

    event = events[0]
    recurrence_overrides = event.get("recurrenceOverrides", {}) or {}
    recurrence_overrides.setdefault(recurrence_id, {}).update({"excluded": True})

    return ctx.update(
        "CalendarEvent",
        {id: {"recurrenceOverrides": recurrence_overrides, "updated": utcnow()}},
        sendSchedulingMessages=send_scheduling_messages,
    )


# -- event notifications ---------------------------------------------------- #
def get_event_notifications(
    ctx: JMAPContext, ids: list[str] | None = None, properties: list[str] | None = None
) -> list[dict]:
    """Gets calendar event notifications, always naming the properties: Stalwart returns a
    reduced default set when a get omits them, silently dropping event/eventPatch."""

    return ctx.get_all(
        "CalendarEventNotification", ids, properties=properties or EVENT_NOTIFICATION_PROPERTIES
    )


def query_event_notifications(
    ctx: JMAPContext,
    filter: dict | None = None,
    position: int = 0,
    limit: int = 50,
    sort: list[dict] | None = None,
) -> dict:
    """Queries calendar event notifications, oldest first by default, returning `{"ids", "total"}`."""

    return ctx.query(
        "CalendarEventNotification",
        filter,
        position,
        limit,
        sort or [{"property": "created", "isAscending": True}],
    )


# -- participant identities -------------------------------------------------- #
def _participant_identity_payload(participant_identity: dict) -> dict:
    return {
        "name": participant_identity["name"],
        "calendarAddress": f"mailto:{participant_identity['email']}",
    }


def create_participant_identities(ctx: JMAPContext, participant_identities: list[dict]) -> dict:
    """Creates participant identities from simplified dicts, honoring is_default via
    onSuccessSetIsDefault."""

    payload = {}
    kwargs = {}
    for participant_identity in participant_identities:
        payload[participant_identity["creation_id"]] = _participant_identity_payload(participant_identity)

        if bool(participant_identity.get("is_default") or False):
            kwargs["onSuccessSetIsDefault"] = f"#{participant_identity['creation_id']}"

    return ctx.create("ParticipantIdentity", payload, **kwargs)


def update_participant_identities(ctx: JMAPContext, participant_identities: list[dict]) -> dict:
    """Updates participant identities from simplified dicts, honoring is_default via
    onSuccessSetIsDefault."""

    payload = {}
    kwargs = {}
    for participant_identity in participant_identities:
        payload[participant_identity["id"]] = _participant_identity_payload(participant_identity)

        if bool(participant_identity.get("is_default") or False):
            kwargs["onSuccessSetIsDefault"] = participant_identity["id"]

    return ctx.update("ParticipantIdentity", payload, **kwargs)


def get_default_participant_identity(ctx: JMAPContext, raise_exception: bool = False) -> str | None:
    """Returns the email address of the default participant identity, or None if not found."""

    for identity in ctx.participant_identities:
        if identity.get("isDefault", False):
            return identity["calendarAddress"].lower().replace("mailto:", "")

    if raise_exception:
        raise ValueError("No default participant identity found.")


# -- principals -------------------------------------------------------------- #
def update_principals(ctx: JMAPContext, principals: list[dict]) -> dict:
    """Updates principals from simplified dicts (id, name, description, time_zone)."""

    payload = {
        principal["id"]: {
            "name": principal.get("name") or None,
            "description": principal.get("description") or None,
            "timeZone": principal.get("time_zone") or None,
        }
        for principal in principals
    }

    return ctx.update("Principal", payload)


def get_principal_availability(
    ctx: JMAPContext,
    principal_id: str,
    utc_start: str,
    utc_end: str,
    show_details: bool = False,
    event_properties: list[str] | None = None,
) -> dict:
    """Gets a principal's availability, optionally with per-event details."""

    payload = {
        "id": principal_id,
        "start": utc_start,
        "end": utc_end,
        "showDetails": show_details,
    }

    if show_details and event_properties:
        payload["eventProperties"] = event_properties

    return ctx.call("Principal/getAvailability", payload)


# -- payload builders --------------------------------------------------------- #
def _get_locations_map(locations: list[dict] | None = None) -> dict[str, dict] | None:
    if locations:
        locations_map = {}
        for location in locations:
            uid = location.get("uid") or str(uuid7())
            locations_map[uid] = {
                "@type": "Location",
                "name": location.get("name"),
            }

        return locations_map


def _get_links_map(links: list[dict] | None = None) -> dict[str, dict] | None:
    if links:
        links_map = {}
        for link in links:
            uid = link.get("uid") or str(uuid7())
            links_map[uid] = {
                "@type": "Link",
                "href": link.get("href"),
                "contentType": link.get("content_type"),
            }

        return links_map


def _get_alerts_map(alerts: list[dict] | None = None) -> dict[str, dict] | None:
    if alerts:
        alerts_map = {}
        for alert in alerts:
            if alert["type"] == "OffsetTrigger":
                trigger = {
                    "@type": "OffsetTrigger",
                    "relativeTo": alert["relative_to"].lower(),
                    "offset": alert["offset"].upper(),
                }
            elif alert["type"] == "AbsoluteTrigger":
                # The API listens UTC: a naive value is read as UTC and sent as ``...Z``.
                trigger = {
                    "@type": "AbsoluteTrigger",
                    "when": normalize_utc_z(alert["when"]),
                }
            else:
                continue

            uid = alert.get("uid") or str(uuid7())
            alerts_map[uid] = {
                "@type": "Alert",
                "action": alert["action"].lower(),
                "trigger": trigger,
            }

        return alerts_map


def _get_participants_map(participants: list[dict] | None = None) -> dict[str, dict] | None:
    """Helper function to construct the 'participants' property map for a calendar event based on the provided list of participant dictionaries."""

    if participants:
        participants_map = {}
        for participant in participants:
            email = participant["email"].lower()
            uid = participant.get("uid") or str(uuid7())
            expect_reply = participant.get("expect_reply", False)
            calendar_address = f"mailto:{email}" if email else None

            if expect_reply:
                send_to = (
                    participant.get("send_to") or {"imip": calendar_address} if calendar_address else None
                )
                schedule_id = participant.get("schedule_id") or calendar_address
            else:
                send_to = None
                schedule_id = None

            participants_map[uid] = {
                "@type": "Participant",
                "name": participant.get("name") or email,
                "sendTo": send_to,
                "scheduleId": schedule_id,
                "calendarAddress": calendar_address,
                "kind": participant.get("kind", "").lower() or None,
                "description": participant.get("description") or None,
                "roles": participant.get("roles") or None,
                "participationStatus": participant.get("participation_status", "").lower() or None,
                "expectReply": expect_reply,
                "comment": participant.get("comment") or None,
            }

        return participants_map
