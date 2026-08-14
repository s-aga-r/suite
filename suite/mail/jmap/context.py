"""An account-scoped working context on top of jmaplib.

This is deliberately not a JMAP implementation: batching, back-references, ``using`` derivation,
accountId injection, and size gates all come from jmaplib. The context only binds a connection to
one account, converts results to the raw wire dicts the app consumes, and carries the Frappe-side
conveniences (Redis-session drift handling, TTL caches, concurrent blob transfers).
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property
from typing import Any, Literal

from cachetools import TTLCache
from jmap.batch import Batch
from jmap.chunking import ChunkedHandle
from jmap.core.errors import MethodError
from jmap.core.ids import Id
from jmap.core.invocation import Handle

from suite.mail.jmap.client import JMAPConnection


def create_batches(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Helper function to create batches of items for processing."""

    for i in range(0, len(items), size):
        yield items[i : i + size]


def batch_dict(d: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
    """Helper function to split a dictionary into smaller dictionaries of a specified batch size."""

    keys = list(d.keys())
    return [{k: d[k] for k in keys[i : i + batch_size]} for i in range(0, len(keys), batch_size)]


class JMAPContext:
    """Binds a JMAP connection to one account and exposes jmaplib primitives plus generic,
    type-parametrized operations returning raw wire bodies.

    ``account`` is None for user-scoped work (PushSubscription), where requests carry no
    accountId at all.
    """

    _cache = TTLCache(maxsize=1_00_000, ttl=1 * 60 * 60)

    def __init__(self, connection: JMAPConnection, account: str | None) -> None:
        self.connection = connection
        self.account = account

    # -- capability surface -------------------------------------------------- #
    @property
    def caps(self):
        """The resolved capabilities for this context's account (or session-wide when user-scoped)."""

        return self.connection.capabilities_for(self.account)

    @property
    def limits(self):
        """The server limits advertised for this account's core capability."""

        return self.caps.limits

    # -- jmaplib primitives -------------------------------------------------- #
    def batch(self) -> Batch:
        """Opens a batch scoped to this context's account; queue calls on it, then `run` it."""

        return Batch(self.caps)

    def run(self, batch: Batch) -> None:
        """Executes a batch and resyncs local account state if the server session drifted."""

        with self.connection.map_transport_errors():
            self.connection.client.execute(batch)

        if self.connection.client.session_stale:
            self.connection.handle_session_drift()

    @staticmethod
    def read(handle: Handle) -> dict:
        """Returns a handle's raw method-response body, or `{"error": ...}` for a method-level error."""

        try:
            if isinstance(handle, ChunkedHandle):
                return _merge_chunk_bodies(handle)
            return handle.result
        except MethodError as e:
            return {"error": e.arguments}

    def call(self, name: str, args: dict) -> dict:
        """Runs a single method call and returns its raw response body."""

        batch = self.batch()
        handle = batch.add(name, _omit_none(args))
        self.run(batch)
        return self.read(handle)

    # -- generic typed operations -------------------------------------------- #
    def get_all(
        self, type_: str, ids: list[str] | None = None, properties: list[str] | None = None, **kwargs
    ) -> list[dict]:
        """Gets objects of the given type (all of them, or the given ids batched at the server's
        per-get limit) and returns the combined `list`."""

        results = []
        if ids:
            for batch in create_batches(ids, self.limits.max_objects_in_get):
                results.extend(
                    self.call(f"{type_}/get", {"ids": batch, "properties": properties, **kwargs}).get(
                        "list", []
                    )
                )
        else:
            results.extend(
                self.call(f"{type_}/get", {"ids": None, "properties": properties, **kwargs}).get("list", [])
            )

        return results

    def create(self, type_: str, payload: dict[str, dict], **kwargs) -> dict:
        """Creates objects of the given type, batching at the server's per-set limit, and returns
        `{"created", "notCreated"}` (plus `"error"` if a whole call failed)."""

        result = {"created": {}, "notCreated": {}}
        for batch in batch_dict(payload, self.limits.max_objects_in_set):
            body = self.call(f"{type_}/set", {"create": batch, **kwargs})

            if "error" in body:
                result["error"] = body["error"]
                break

            result["created"].update(body.get("created", {}))
            if not_created := body.get("notCreated", {}):
                result["notCreated"].update(not_created)

        return result

    def update(self, type_: str, payload: dict[str, dict], **kwargs) -> dict:
        """Updates objects of the given type, batching at the server's per-set limit, and returns
        `{"updated", "notUpdated"}` (plus `"error"` if a whole call failed)."""

        result = {"updated": [], "notUpdated": {}}
        for batch in batch_dict(payload, self.limits.max_objects_in_set):
            body = self.call(f"{type_}/set", {"update": batch, **kwargs})

            if "error" in body:
                result["error"] = body["error"]
                break

            result["updated"].extend(body.get("updated", {}).keys())
            if not_updated := body.get("notUpdated", {}):
                result["notUpdated"].update(not_updated)

        return result

    def destroy(self, type_: str, ids: list[str], **kwargs) -> dict:
        """Destroys objects of the given type, batching at the server's per-set limit, and returns
        `{"destroyed", "notDestroyed"}` (plus `"error"` if a whole call failed)."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in create_batches(ids, self.limits.max_objects_in_set):
            body = self.call(f"{type_}/set", {"destroy": batch, **kwargs})

            if "error" in body:
                result["error"] = body["error"]
                break

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result

    def query(
        self,
        type_: str,
        filter: dict | None = None,
        position: int = 0,
        limit: int = 50,
        sort: list[dict] | None = None,
        **kwargs,
    ) -> dict:
        """Queries objects of the given type, paginating up to `limit` results, and returns
        `{"ids", "total"}`."""

        ids = []
        total = None
        batch_size = min(limit, self.limits.max_objects_in_get)

        while len(ids) < limit:
            current_batch_size = min(batch_size, limit - len(ids))

            query_response = self.call(
                f"{type_}/query",
                {
                    "filter": filter,
                    "position": position,
                    "limit": current_batch_size,
                    "sort": sort,
                    "calculateTotal": total is None,
                    **kwargs,
                },
            )

            batch_ids = query_response.get("ids", [])
            ids.extend(batch_ids)

            if total is None:
                total = query_response.get("total")

            if len(batch_ids) < current_batch_size or (total is not None and len(ids) >= total):
                break

            position += len(batch_ids)

        return {"ids": ids[:limit], "total": total}

    def changes(self, type_: str, since_state: str) -> dict:
        """Gets changes of the given type since a state.

        A method-level failure (e.g. `forbidden`, `cannotCalculateChanges`) raises instead of
        returning, since every caller reads the body as if it were a changes result.
        """

        body = self.call(f"{type_}/changes", {"sinceState": since_state})

        if "error" in body:
            raise RuntimeError(f"{type_}/changes failed: {body['error']}")

        return body

    # -- blobs ---------------------------------------------------------------- #
    def upload_blob(self, blob: bytes | str, content_type: str = "message/rfc822") -> dict:
        """Uploads a blob and returns the raw response containing the blob ID and other metadata."""

        if isinstance(blob, str):
            blob = blob.encode("utf-8")

        with self.connection.map_transport_errors():
            result = self.connection.client.upload(
                blob, content_type=content_type, account_id=Id(self.account)
            )

        return result.to_wire()

    def upload_blobs_concurrently(self, blobs: list[tuple[bytes | str, str]]) -> list[dict]:
        """Uploads multiple blobs concurrently, given (blob, content_type) tuples, and returns
        the raw responses in input order."""

        count = len(blobs)
        if count == 0:
            return []

        results = [None] * count

        if count == 1:
            blob, content_type = blobs[0]
            return [self.upload_blob(blob, content_type)]

        with ThreadPoolExecutor(max_workers=self.limits.max_concurrent_upload) as executor:
            future_to_index = {}

            for index, (blob, content_type) in enumerate(blobs):
                future = executor.submit(self.upload_blob, blob, content_type)
                future_to_index[future] = index

            for future in as_completed(future_to_index):
                index = future_to_index[future]
                results[index] = future.result()

        return results

    def download_blob(self, blob_id: str, name: str | None = None) -> bytes:
        """Downloads a blob's bytes by its ID, with an optional file name for the response headers."""

        with self.connection.map_transport_errors():
            return self.connection.client.download(
                blob_id, name=name or "blob", account_id=Id(self.account)
            )

    def download_blobs_concurrently(self, blobs: list[tuple[str, str | None]]) -> dict[str, bytes]:
        """Downloads multiple blobs concurrently, given (blob_id, name) tuples, and returns a
        mapping of blob IDs to their content."""

        if len(blobs) == 1:
            blob_id, name = blobs[0]
            return {blob_id: self.download_blob(blob_id, name)}

        results = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self.download_blob, blob_id, name): blob_id for blob_id, name in blobs}
            for future in as_completed(futures):
                blob_id = futures[future]
                results[blob_id] = future.result()

        return results

    # -- account/session accessors -------------------------------------------- #
    @property
    def account_ids(self) -> list[str]:
        """Returns the list of account IDs for the logged-in user."""

        return list(self.connection.accounts.keys())

    @property
    def has_multiple_accounts(self) -> bool:
        """Returns True if the user has multiple accounts, False otherwise."""

        return len(self.account_ids) > 1

    @property
    def personal_account_id(self) -> str | None:
        """Returns the personal account ID for the logged-in user, if any."""

        for account, details in self.connection.accounts.items():
            if details.get("isPersonal"):
                return account

    def primary_account_id(self, urn: str = "urn:ietf:params:jmap:mail") -> str:
        """Returns the primary account ID advertised for the given capability URN."""

        return self.connection.primary_accounts[urn]

    # -- TTL caches ------------------------------------------------------------ #
    @classmethod
    def invalidate_cache(
        cls, account: str | None = None, key: Literal["identities", "mailboxes"] | None = None
    ) -> None:
        """Invalidates the cache for a specific account and key, or for all accounts and keys if no parameters are provided."""

        if account:
            if key:
                if account in cls._cache and key in cls._cache[account]:
                    del cls._cache[account][key]  # Remove the specific key from the account's cache
            else:
                cls._cache.pop(account, None)  # Remove the entire cache for the specified account
        else:
            if key:
                for account_cache in cls._cache.values():  # Remove the specific key from all account caches
                    account_cache.pop(key, None)
            else:
                cls._cache.clear()  # Clear the entire cache for all accounts and keys

    @property
    def cache(self) -> dict:
        """Returns the cache for the current account (or user, for user-scoped contexts)."""

        key = self.account or self.connection.user
        if key not in self._cache:
            self._cache[key] = {}

        return self._cache[key]

    @property
    def identities(self) -> list[dict]:
        """Returns the list of identities for the account, using caching to optimize performance."""

        if identities := self.cache.get("identities"):
            return identities

        identities = self.get_all("Identity")
        self.cache["identities"] = identities

        return identities

    @property
    def mailboxes(self) -> list[dict]:
        """Returns the list of mailboxes for the account, using caching to optimize performance."""

        if mailboxes := self.cache.get("mailboxes"):
            return mailboxes

        mailboxes = self.get_all("Mailbox")
        self.cache["mailboxes"] = mailboxes

        return mailboxes

    @cached_property
    def address_books(self) -> list[dict]:
        """Returns the list of address books for the account, cached per context."""

        return self.get_all("AddressBook")

    @cached_property
    def calendars(self) -> list[dict]:
        """Returns the list of calendars for the account, cached per context."""

        return self.get_all("Calendar")

    @cached_property
    def participant_identities(self) -> list[dict]:
        """Returns the list of participant identities for the account, cached per context."""

        return self.get_all("ParticipantIdentity")


def _merge_chunk_bodies(handle: ChunkedHandle) -> dict:
    """Merges the raw bodies of an auto-chunked '/get' back into one response body.

    Callers batch ids at `max_objects_in_get`, so chunking only triggers as a safety net when a
    server lowers its limit between the read and the request.
    """

    merged: dict = {}
    for chunk in handle.chunks:
        for key, value in chunk.result.items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key].extend(value)
            else:
                merged.setdefault(key, value)

    return merged


def _omit_none(args: dict) -> dict:
    """Drops top-level None arguments (an omitted JMAP argument, not an explicit null)."""

    return {k: v for k, v in args.items() if v is not None}


def format_jmap_error(error: dict | None) -> str:
    """Returns a readable message for a JMAP error object.

    Only `type` is mandatory on a JMAP error object; `description` is optional and may be null,
    so never index into it directly.
    """

    from frappe import _

    error = error or {}

    return error.get("description") or error.get("type") or _("An unknown error occurred.")


def get_jmap_set_error_message(response: dict, not_done_key: str, id: str) -> str:
    """Returns a readable message for a failed JMAP `set` call.

    A `set` can fail per object (reported under `not_done_key`, keyed by the object id) or at the
    method level (reported under `error`), and neither is guaranteed to be present — nor is the
    per-object error guaranteed to be keyed by the id we asked about — so every source is probed
    before falling back to a generic message.
    """

    not_done = response.get(not_done_key) or {}
    error = not_done.get(id) or next(iter(not_done.values()), None) or response.get("error")

    return format_jmap_error(error)
