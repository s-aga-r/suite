from collections.abc import Iterator
from typing import Any, ClassVar

import frappe
from frappe import _

from suite.mail.stalwart.transport import JMAPConnection
from suite.mail.utils import log_mail_error

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MANAGEMENT_CAPABILITY = "urn:stalwart:jmap"

# Shown to the user for any Stalwart failure; the real cause is written to the error log instead.
GENERIC_ERROR = "The mail server could not process this request. Please try again or contact your administrator if the problem persists."


class Serializable:
    """Interface for objects that can be turned into a JMAP object body."""

    def to_dict(self) -> dict:
        """Returns the JMAP wire representation of the object."""

        raise NotImplementedError


class ManagementService:
    """Base for every Stalwart directory resource.

    The Stalwart management API is a JMAP dialect: it is reached with the ``urn:stalwart:jmap``
    capability, every method name is prefixed with ``x:`` (e.g. ``x:Account/get``) and, unlike the
    per-mailbox JMAP API, calls are server-scoped and carry no ``accountId``. All CRUD lives here so
    each resource subclass only declares its ``type`` and any resource-specific helpers.
    """

    type: ClassVar[str] = ""
    default_properties: ClassVar[list[str] | None] = None
    # Set by resources the server will only page with a cursor, never an offset.
    cursor_paginated: ClassVar[bool] = False

    def __init__(self, connection: JMAPConnection) -> None:
        """Binds the service to an admin-authenticated management connection."""

        if not self.type:
            raise NotImplementedError("A ManagementService subclass must set 'type'.")

        self.connection = connection

    # --- capability limits -------------------------------------------------

    @property
    def _core(self) -> dict:
        """Returns the server's advertised core JMAP capability object."""

        return self.connection.capabilities.get(CORE_CAPABILITY) or {}

    @property
    def account_id(self) -> str:
        """Returns the account id management calls are scoped to (the admin's own account).

        Stalwart advertises the management capability per-account, so the id comes from the
        session's primary account for ``urn:stalwart:jmap``.
        """

        return self.connection.primary_accounts[MANAGEMENT_CAPABILITY]

    @property
    def max_objects_in_get(self) -> int:
        """Returns the most objects the server allows in one ``get`` call."""

        return self._core.get("maxObjectsInGet") or 500

    @property
    def max_objects_in_set(self) -> int:
        """Returns the most objects the server allows in one ``set`` call."""

        return self._core.get("maxObjectsInSet") or 500

    @staticmethod
    def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
        """Yields ``items`` in lists of at most ``size``."""

        for i in range(0, len(items), size):
            yield items[i : i + size]

    @staticmethod
    def _chunk_dict(d: dict[str, Any], size: int) -> Iterator[dict[str, Any]]:
        """Yields ``d`` in sub-dicts of at most ``size`` entries."""

        keys = list(d)
        for i in range(0, len(keys), size):
            yield {k: d[k] for k in keys[i : i + size]}

    # --- transport ---------------------------------------------------------

    def _fail(self, detail: str) -> None:
        """Logs the real Stalwart error and raises a generic message so it is never shown to the user."""

        log_mail_error(title=_("Stalwart {0} request failed").format(self.type), message=detail)
        frappe.throw(_(GENERIC_ERROR))

    def _validate_capabilities(self) -> None:
        """Ensures the connected account can use the management capability.

        The management capability is advertised per-account (in ``primaryAccounts``), not in the
        server-wide ``capabilities`` block.
        """

        if MANAGEMENT_CAPABILITY not in self.connection.primary_accounts:
            self._fail(f"Server does not advertise {MANAGEMENT_CAPABILITY} in primaryAccounts.")

    def _method(self, action: str) -> str:
        """Returns the ``x:``-prefixed JMAP method name for ``action`` (e.g. ``x:Account/get``)."""

        return f"x:{self.type}/{action}"

    def _call(self, method_calls: list[list]) -> list[dict]:
        """Sends one JMAP request and returns the unwrapped args of each method response in order."""

        self._validate_capabilities()

        try:
            response = self.connection.request(
                method="POST",
                url=self.connection.api_url,
                headers={"Content-Type": "application/json"},
                json={"using": [CORE_CAPABILITY, MANAGEMENT_CAPABILITY], "methodCalls": method_calls},
                return_json=True,
            )
        except Exception as e:
            self._fail(str(e))

        session_state = response.get("sessionState")
        if session_state and self.connection.state != session_state:
            self.connection._session_discovery()

        results = []
        for method_response in response.get("methodResponses", []):
            name, args = method_response[0], method_response[1]
            if name == "error":
                self._fail(frappe.as_json(args))
            results.append(args)

        return results

    def _invoke(self, action: str, **args) -> dict:
        """Runs a single method call for this resource, dropping ``None`` arguments."""

        args = {k: v for k, v in args.items() if v is not None}
        args["accountId"] = self.account_id
        return self._call([[self._method(action), args, "0"]])[0]

    def _set(self, **args) -> dict:
        """Runs a ``set`` call and raises if the server reports any object it could not mutate."""

        result = self._invoke("set", **args)
        for key in ("notCreated", "notUpdated", "notDestroyed"):
            if failures := result.get(key):
                self._fail(frappe.as_json({key: failures}))
        return result

    def _resolve_properties(self, properties: list[str] | None) -> list[str] | None:
        """Falls back to the resource's ``default_properties`` when ``properties`` is not given."""

        return properties if properties is not None else self.default_properties

    # --- read --------------------------------------------------------------

    def get(self, id: str, properties: list[str] | None = None) -> dict | None:
        """Returns a single object by id, or ``None`` if it does not exist."""

        result = self._invoke("get", ids=[id], properties=self._resolve_properties(properties))
        objects = result.get("list") or []
        return objects[0] if objects else None

    def get_all(
        self,
        filter: dict | None = None,
        sort: list[dict] | None = None,
        limit: int | None = None,
        properties: list[str] | None = None,
    ) -> list[dict]:
        """Returns every matching object in a single request.

        With no filter, one ``get`` (ids omitted) returns all objects. With a filter, ``query`` and
        ``get`` are chained through a JMAP result reference so the query's ids feed the get without a
        second round-trip.
        """

        properties = self._resolve_properties(properties)

        if filter is None and sort is None and limit is None:
            return self._invoke("get", properties=properties).get("list") or []

        query_args = {"accountId": self.account_id}
        query_args.update(
            {k: v for k, v in {"filter": filter, "sort": sort, "limit": limit}.items() if v is not None}
        )
        get_args = {
            "accountId": self.account_id,
            "#ids": {"resultOf": "q", "name": self._method("query"), "path": "/ids"},
        }
        if properties is not None:
            get_args["properties"] = properties

        responses = self._call(
            [[self._method("query"), query_args, "q"], [self._method("get"), get_args, "g"]]
        )
        return responses[1].get("list") or []

    def query(
        self,
        filter: dict | None = None,
        sort: list[dict] | None = None,
        position: int = 0,
        limit: int | None = None,
        anchor: str | None = None,
    ) -> dict:
        """Returns ``{"ids", "total"}`` without fetching the objects, paging until ``limit``/exhausted.

        Pages by offset (``position``) unless the resource is ``cursor_paginated`` or an ``anchor`` is
        given, in which case each page starts *after* that id — the anchor is exclusive, and the server
        ignores any offset alongside it.
        """

        ids: list[str] = []
        total: int | None = None
        page = self.max_objects_in_get
        by_cursor = self.cursor_paginated or anchor is not None
        cursor = anchor

        while True:
            take = page if limit is None else min(page, limit - len(ids))
            paging = {"anchor": cursor} if by_cursor else {"position": position}
            result = self._invoke(
                "query", filter=filter, sort=sort, limit=take, calculateTotal=total is None, **paging
            )

            batch = result.get("ids") or []
            ids.extend(batch)
            if total is None:
                total = result.get("total")

            if by_cursor:
                cursor = batch[-1] if batch else cursor
            else:
                position += len(batch)
            done = (
                not batch
                or (total is not None and len(ids) >= total)
                or (limit is not None and len(ids) >= limit)
            )
            if done:
                break

        return {"ids": ids if limit is None else ids[:limit], "total": total}

    def find(self, filter: dict, properties: list[str] | None = None) -> dict | None:
        """Returns the first object matching ``filter``, or ``None``."""

        matches = self.get_all(filter=filter, limit=1, properties=properties)
        return matches[0] if matches else None

    def list_page(
        self,
        filter: dict | None = None,
        sort: list[dict] | None = None,
        position: int = 0,
        limit: int | None = None,
        properties: list[str] | None = None,
        anchor: str | None = None,
    ) -> dict:
        """Returns ``{"items", "total"}`` — one page of full objects in query order.

        Unlike ``get_all`` (which fetches everything), this pages via ``position``/``limit`` for
        large collections such as the queue, or via ``anchor`` for cursor-only ones such as the log,
        and preserves the query's ordering.
        """

        page = self.query(filter=filter, sort=sort, position=position, limit=limit, anchor=anchor)
        ids = page["ids"]
        items: list[dict] = []
        if ids:
            result = self._invoke("get", ids=ids, properties=self._resolve_properties(properties))
            by_id = {obj["id"]: obj for obj in (result.get("list") or [])}
            items = [by_id[id] for id in ids if id in by_id]

        return {"items": items, "total": page["total"]}

    # --- write -------------------------------------------------------------

    def _create(self, obj: Serializable) -> dict:
        """Creates one object and returns the created object including server-set fields."""

        return self._set(create={"0": obj.to_dict()})["created"]["0"]

    def create(self, obj: Serializable) -> str:
        """Creates one object and returns its server-assigned id."""

        return self._create(obj)["id"]

    def create_many(self, objs: list[Serializable]) -> list[str]:
        """Creates objects in batches and returns their ids in the same order."""

        created: dict[str, dict] = {}
        payload = {str(i): obj.to_dict() for i, obj in enumerate(objs)}
        for batch in self._chunk_dict(payload, self.max_objects_in_set):
            created.update(self._set(create=batch)["created"])

        return [created[str(i)]["id"] for i in range(len(objs))]

    def update(self, id: str, patch: dict) -> None:
        """Applies a partial update. ``patch`` keys may be property names or JSON-pointer paths."""

        self._set(update={id: patch})

    def update_many(self, patches: dict[str, dict]) -> None:
        """Applies partial updates to many objects (``{id: patch}``) in batches."""

        for batch in self._chunk_dict(patches, self.max_objects_in_set):
            self._set(update=batch)

    def set_aliases(self, id: str, aliases: list[Serializable]) -> None:
        """Replaces the object's aliases with the given set (index-keyed map on the wire).

        Shared by resources that carry an ``aliases`` map (accounts, groups, mailing lists).
        """

        self.update(id, {"aliases": {f"{idx}": alias.to_dict() for idx, alias in enumerate(aliases)}})

    def delete(self, ids: str | list[str]) -> None:
        """Deletes one id or a list of ids in batches."""

        ids = [ids] if isinstance(ids, str) else list(ids)
        for batch in self._chunks(ids, self.max_objects_in_set):
            self._set(destroy=batch)

    def changes(self, since_state: str) -> dict:
        """Returns the objects changed since ``since_state`` for delta sync."""

        return self._invoke("changes", sinceState=since_state)
