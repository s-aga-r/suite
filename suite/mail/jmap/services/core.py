from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property
from typing import Any, ClassVar, Literal

from cachetools import TTLCache
from jmap.batch import Batch
from jmap.chunking import ChunkedHandle
from jmap.core.errors import MethodError
from jmap.core.ids import Id
from jmap.core.invocation import Handle

from suite.mail.jmap.client import JMAPConnection


class CoreServiceHelper:
    """Helper class for CoreService, providing utility methods for batch processing."""

    @staticmethod
    def create_batches(items: list[Any], size: int) -> Iterator[list[Any]]:
        """Helper method to create batches of items for processing."""

        for i in range(0, len(items), size):
            yield items[i : i + size]

    @staticmethod
    def batch_dict(d: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
        """Helper method to split a dictionary into smaller dictionaries of a specified batch size."""

        keys = list(d.keys())
        return [{k: d[k] for k in keys[i : i + batch_size]} for i in range(0, len(keys), batch_size)]


class CallIdGenerator:
    """Utility class to generate unique call IDs for JMAP method calls."""

    def __init__(self, start: int = 0) -> None:
        """Initializes the CallIdGenerator with an optional starting value for the call ID counter."""

        self._value = start

    def next(self) -> str:
        """Generates the next unique call ID as a string, incrementing the internal counter for each call."""

        val = str(self._value)
        self._value += 1
        return val


class CoreService(CoreServiceHelper):
    """Core service for JMAP operations, providing common functionality and properties."""

    _cache = TTLCache(maxsize=1_00_000, ttl=1 * 60 * 60)

    capabilities: ClassVar[list[str]] = ["urn:ietf:params:jmap:core"]

    # PushSubscription requests are user-scoped and carry no accountId; that service flips this off.
    account_scoped: ClassVar[bool] = True

    def __init__(self, account: str, connection: JMAPConnection) -> None:
        """Initializes the CoreService with the provided account ID and JMAP connection.

        The connection carries the authenticated user; only the JMAP ``account`` is needed
        to scope requests (and as the cache key, since account data is shared across the users
        who have access to it)."""

        self.account = account
        self.connection = connection

    def __post_init__(self) -> None:
        """Post-initialization to check if the JMAP server supports the required capabilities and raise an error if not."""

        if "urn:ietf:params:jmap:core" not in self.connection.capabilities:
            raise NotImplementedError("The JMAP server does not support the Core capability.")

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
    def _type(self) -> str:
        """Returns the type of objects managed by this service, based on the 'type' class variable defined in subclasses."""

        if not hasattr(self, "type") or not isinstance(self.type, str):
            raise NotImplementedError(
                "Subclasses of CoreService must define a 'type' class variable of type str."
            )

        return self.type

    @property
    def cache(self) -> dict:
        """Returns the cache for the current account, creating a new cache entry if it does not exist."""

        if self.account in self._cache:
            return self._cache[self.account]

        self._cache[self.account] = {}

        return self._cache[self.account]

    @property
    def _caps(self):
        """The resolved capabilities for this service's account (or session-wide when user-scoped)."""

        return self.connection.capabilities_for(self.account if self.account_scoped else None)

    @property
    def core(self) -> dict:
        """Returns the core capabilities of the JMAP server."""

        return self.connection.capabilities["urn:ietf:params:jmap:core"]

    @property
    def max_calls_in_request(self) -> int:
        """Returns the maximum number of method calls allowed in a single JMAP request."""

        return self._caps.limits.max_calls_in_request

    @property
    def max_size_upload(self) -> int:
        """Returns the maximum size of an upload allowed by the JMAP server, in bytes."""

        return self._caps.limits.max_size_upload

    @property
    def max_concurrent_upload(self) -> int:
        """Returns the maximum number of concurrent uploads allowed by the JMAP server."""

        return self._caps.limits.max_concurrent_upload

    @property
    def max_size_request(self) -> int:
        """Returns the maximum size of a JMAP request allowed by the server, in bytes."""

        return self._caps.limits.max_size_request

    @property
    def max_concurrent_requests(self) -> int:
        """Returns the maximum number of concurrent requests allowed by the JMAP server."""

        return self._caps.limits.max_concurrent_requests

    @property
    def max_objects_in_get(self) -> int:
        """Returns the maximum number of objects that can be retrieved in a single JMAP 'get' method call."""

        return self._caps.limits.max_objects_in_get

    @property
    def max_objects_in_set(self) -> int:
        """Returns the maximum number of objects that can be created, updated, or destroyed in a single JMAP 'set' method call."""

        return self._caps.limits.max_objects_in_set

    @property
    def account_ids(self) -> list[str]:
        """Returns the list of account IDs for the logged-in user."""

        return list(self.connection.accounts.keys())

    @property
    def has_multiple_accounts(self) -> bool:
        """Returns True if the user has multiple accounts, False otherwise."""

        return len(self.account_ids) > 1

    @property
    def primary_account_id(self) -> str:
        """Returns the primary account ID for the logged-in user."""

        return self.connection.primary_accounts["urn:ietf:params:jmap:mail"]

    @property
    def personal_account_id(self) -> str | None:
        """Returns the personal account ID for the logged-in user, if any, or None if no personal account is found."""

        for account, details in self.connection.accounts.items():
            if details.get("isPersonal"):
                return account

    @property
    def identities(self) -> list[dict]:
        """Returns the list of identities for the account, using caching to optimize performance."""

        if identities := self.cache.get("identities"):
            return identities

        from suite.mail.jmap.services.mail.identity import IdentityService

        identities = IdentityService(self.account, self.connection).get()
        self.cache["identities"] = identities

        return identities

    @property
    def mailboxes(self) -> list[dict]:
        """Returns the list of mailboxes for the account, using caching to optimize performance."""

        if mailboxes := self.cache.get("mailboxes"):
            return mailboxes

        from suite.mail.jmap.services.mail.mailbox import MailboxService

        mailboxes = MailboxService(self.account, self.connection).get()
        self.cache["mailboxes"] = mailboxes

        return mailboxes

    @cached_property
    def address_books(self) -> list[dict]:
        """Returns the list of address books for the account, using caching to optimize performance."""

        from suite.mail.jmap.services.contacts.address_book import AddressBookService

        return AddressBookService(self.account, self.connection).get()

    @cached_property
    def calendars(self) -> list[dict]:
        """Returns the list of calendars for the account, using caching to optimize performance."""

        from suite.mail.jmap.services.calendars.calendar import CalendarService

        return CalendarService(self.account, self.connection).get()

    @cached_property
    def participant_identities(self) -> list[dict]:
        """Returns the list of participant identities for the account, using caching to optimize performance."""

        from suite.mail.jmap.services.calendars.participant_identity import ParticipantIdentityService

        return ParticipantIdentityService(self.account, self.connection).get()

    def validate_capabilities(self, required_capabilities: list[str], raise_exception: bool = False) -> bool:
        """Validates that the required capabilities are supported by the JMAP server."""

        capabilities = self.connection.capabilities

        for capability in required_capabilities:
            if capability not in capabilities:
                if raise_exception:
                    raise ValueError(
                        f"Required capability '{capability}' is not supported by the JMAP server."
                    )

                return False

        return True

    def validate_method_calls(
        self, method_calls: list[list[str | dict]], raise_exception: bool = False
    ) -> bool:
        """Validates the format and content of the method calls in a JMAP request."""

        if not method_calls or len(method_calls) > self.max_calls_in_request:
            if raise_exception:
                raise ValueError("Invalid number of method calls.")
            return False

        call_ids = []
        for method_call in method_calls:
            if not isinstance(method_call, list) or len(method_call) != 3:
                if raise_exception:
                    raise ValueError("Invalid method call format.")
                return False

            if (
                not isinstance(method_call[0], str)
                or not isinstance(method_call[1], dict)
                or not isinstance(method_call[2], str)
            ):
                if raise_exception:
                    raise ValueError("Invalid method call format.")
                return False

            if method_call[2] in call_ids:
                if raise_exception:
                    raise ValueError("Duplicate method call ID.")
                return False

            call_ids.append(method_call[2])

        return True

    def _call(self, capabilities: list[str], method_calls: list[list[str | dict]], **kwargs) -> dict:
        """Sends a JMAP request to the server with the specified capabilities and method calls."""

        self.validate_capabilities(capabilities, raise_exception=True)
        self.validate_method_calls(method_calls, raise_exception=True)

        payload = {"using": capabilities, "methodCalls": method_calls}

        response = self.connection.request(
            method="POST",
            url=self.connection.api_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            return_json=True,
            **kwargs,
        )

        session_state = response["sessionState"]
        if self.connection.state != session_state:
            self.connection.handle_session_drift()

        return response

    def _exec(self, action: Literal["get", "set", "query", "changes", "upload", "lookup"], **payload) -> dict:
        payload = {**{k: v for k, v in payload.items() if v is not None}}

        if self._type != "PushSubscription":
            payload["accountId"] = self.account

        return self._call(
            capabilities=self.capabilities,
            method_calls=[[f"{self._type}/{action}", payload, "0"]],
        )

    def _new_batch(self) -> Batch:
        """Opens a batch scoped to this service's account; queue calls on it, then `_run` it."""

        return Batch(self._caps)

    def _run(self, batch: Batch) -> None:
        """Executes a batch and resyncs local account state if the server session drifted."""

        with self.connection.map_transport_errors():
            self.connection.client.execute(batch)

        if self.connection.client.session_stale:
            self.connection.handle_session_drift()

    @staticmethod
    def _read(handle: Handle) -> dict:
        """Returns a handle's raw method-response body, or `{"error": ...}` for a method-level error."""

        try:
            if isinstance(handle, ChunkedHandle):
                return CoreService._merge_chunk_bodies(handle)
            return handle.result
        except MethodError as e:
            return {"error": e.arguments}

    @staticmethod
    def _merge_chunk_bodies(handle: ChunkedHandle) -> dict:
        """Merges the raw bodies of an auto-chunked '/get' back into one response body.

        Services pre-batch ids at `max_objects_in_get`, so chunking only triggers as a safety net
        when a server lowers its limit between the read and the request.
        """

        merged: dict = {}
        for chunk in handle.chunks:
            for key, value in chunk.result.items():
                if isinstance(value, list) and isinstance(merged.get(key), list):
                    merged[key].extend(value)
                else:
                    merged.setdefault(key, value)

        return merged

    @staticmethod
    def _omit_none(args: dict) -> dict:
        """Drops top-level None arguments (an omitted JMAP argument, not an explicit null)."""

        return {k: v for k, v in args.items() if v is not None}

    def _call_one(self, name: str, args: dict) -> dict:
        """Queues a single method call, executes it, and returns its raw response body."""

        batch = self._new_batch()
        handle = batch.add(name, self._omit_none(args))
        self._run(batch)
        return self._read(handle)

    def call(self, name: str, args: dict) -> dict:
        """Public escape hatch for methods without a dedicated wrapper (e.g. Email/import),
        returning the raw response body."""

        return self._call_one(name, args)

    def _create(self, create: dict, **kwargs) -> dict:
        """Internal method to create objects of the specified type using the JMAP 'set' method."""

        return self._call_one(f"{self._type}/set", {"create": create, **kwargs})

    def _get(self, ids: list[str] | None = None, properties: list[str] | None = None, **kwargs) -> dict:
        """Internal method to get objects of the specified type using the JMAP 'get' method, optionally filtering by a list of IDs."""

        return self._call_one(f"{self._type}/get", {"ids": ids, "properties": properties, **kwargs})

    def _update(self, update: dict, **kwargs) -> dict:
        """Internal method to update objects of the specified type using the JMAP 'set' method."""

        return self._call_one(f"{self._type}/set", {"update": update, **kwargs})

    def _delete(self, destroy: list[str], **kwargs) -> dict:
        """Internal method to delete objects of the specified type using the JMAP 'set' method, filtering by a list of IDs."""

        return self._call_one(f"{self._type}/set", {"destroy": destroy, **kwargs})

    def _query(
        self,
        filter: dict | None = None,
        position: int = 0,
        limit: int = 50,
        sort: list[dict] | None = None,
        calculate_total: bool = True,
        **kwargs,
    ) -> dict:
        """Internal method to query objects of the specified type using the JMAP 'query' method, with support for filtering, sorting, and pagination."""

        return self._call_one(
            f"{self._type}/query",
            {
                "filter": filter,
                "position": position,
                "limit": limit,
                "sort": sort,
                "calculateTotal": calculate_total,
                **kwargs,
            },
        )

    def _changes(self, since_state: str) -> dict:
        """Internal method to get changes of the specified type since a given state using the JMAP 'changes' method.

        A method-level failure (e.g. `forbidden`, `cannotCalculateChanges`) raises instead of
        returning, since every caller reads the body as if it were a changes result.
        """

        body = self._call_one(f"{self._type}/changes", {"sinceState": since_state})

        if "error" in body:
            raise RuntimeError(f"{self._type}/changes failed: {body['error']}")

        return body

    def upload_blob(self, blob: bytes | str, content_type: str = "message/rfc822") -> dict:
        """Uploads a blob to the JMAP server using the upload URL, and returns the response containing the blob ID and other metadata."""

        if isinstance(blob, str):
            blob = blob.encode("utf-8")

        with self.connection.map_transport_errors():
            result = self.connection.client.upload(
                blob, content_type=content_type, account_id=Id(self.account)
            )

        return result.to_wire()

    def upload_blobs_concurrently(self, blobs: list[tuple[bytes | str, str]]) -> list[dict]:
        """Uploads multiple blobs concurrently to the JMAP server, given a list of tuples containing the blob data and content type, and returns a list of responses containing the blob IDs and other metadata."""

        count = len(blobs)
        if count == 0:
            return []

        results = [None] * count

        if count == 1:
            blob, content_type = blobs[0]
            return [self.upload_blob(blob, content_type)]

        with ThreadPoolExecutor(max_workers=self.max_concurrent_upload) as executor:
            future_to_index = {}

            for index, (blob, content_type) in enumerate(blobs):
                future = executor.submit(self.upload_blob, blob, content_type)
                future_to_index[future] = index

            for future in as_completed(future_to_index):
                index = future_to_index[future]
                results[index] = future.result()

        return results

    def download_blob(self, blob_id: str, name: str | None = None) -> bytes:
        """Downloads a blob from the JMAP server using the download URL, given the blob ID and an optional name for the downloaded file, and returns the content of the blob as bytes."""

        with self.connection.map_transport_errors():
            return self.connection.client.download(
                blob_id, name=name or "blob", account_id=Id(self.account)
            )

    def download_blobs_concurrently(self, blobs: list[tuple[str, str | None]]) -> dict[str, bytes]:
        """Downloads multiple blobs concurrently from the JMAP server, given a list of tuples containing blob IDs and optional names, and returns a dictionary mapping blob IDs to their downloaded content."""

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
