import dataclasses
import time
from contextlib import contextmanager
from functools import cached_property
from urllib.parse import urljoin

import httpx
from jmap.auth import BasicAuth
from jmap.capabilities.registry import ActiveCapabilities, Registry
from jmap.client import JMAPClient
from jmap.core.errors import RequestError, TransportError
from jmap.core.ids import Id
from jmap.core.retry import RetryPolicy
from jmap.core.session import Session
from jmap.defaults import default_registry

from suite.mail.jmap.connection import (
    UNAVAILABLE_STATUS_CODES,
    JMAPConnectionInfo,
    JMAPSessionManager,
    MailServerUnavailableError,
)


class _RawResult:
    """Duck-types the model surface jmaplib's response parsing expects, returning the wire dict untouched."""

    @staticmethod
    def model_validate(arguments: dict) -> dict:
        return arguments


def _build_raw_registry() -> Registry:
    """jmaplib's default registry with every method's response left as its raw wire dict.

    Suite services consume raw JMAP bodies. Overriding only ``response_model`` keeps all other
    spec-driven behavior intact: ``using`` derivation, account scoping, mutation classification,
    and batch size gates.
    """

    registry = Registry()
    base = default_registry()

    for urn in base.urns:
        for spec in base.specs_for(urn):
            registry.register(
                dataclasses.replace(
                    spec,
                    methods=tuple(
                        dataclasses.replace(method, response_model=_RawResult) for method in spec.methods
                    ),
                )
            )

    return registry


RAW_REGISTRY = _build_raw_registry()


class JMAPConnection:
    """A JMAP server connection backed by jmaplib, exposing a :class:`jmap.client.JMAPClient`.

    Keeps the raw session dict available (services and account syncing consume it as-is) and
    persists it through the session manager so later connections skip the discovery round trip.
    """

    def __init__(
        self,
        info: JMAPConnectionInfo,
        session_manager: JMAPSessionManager | None = None,
        user: str | None = None,
    ) -> None:
        """Initializes the JMAPConnection with the provided connection information.

        ``user`` is the Frappe user the connection is authenticated as, retained so callers can
        resync per-user state (e.g. JMAP Accounts) when the server session state changes.
        """

        self._info = info
        self._session_manager = session_manager
        self.user = user
        self._capabilities_cache: dict[str | None, ActiveCapabilities] = {}
        self._http = httpx.Client(
            auth=BasicAuth(info.username, info.password),
            verify=info.verify_ssl,
            # httpx does not follow redirects by default, but session discovery depends on it:
            # /.well-known/jmap is a redirect on every real server (Stalwart answers 307).
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=info.timeout[0],
                read=info.timeout[1],
                write=info.timeout[1],
                pool=info.timeout[0],
            ),
        )

        self._initialize_session()

    @property
    def _well_known_url(self) -> str:
        return urljoin(self._info.url, "/.well-known/jmap")

    def _initialize_session(self) -> None:
        """Initializes the session by attempting to retrieve it from the session manager or performing session discovery."""

        if self._session_manager:
            cached = self._session_manager.get_session()
            # Entries persisted before the jmaplib migration lack `base_url` and cannot resolve
            # relative endpoint URLs; treat them as absent so the cache self-migrates.
            if cached and cached.get("base_url"):
                try:
                    self._session_obj = Session.from_wire(
                        {k: v for k, v in cached.items() if k not in ("timestamp", "base_url")},
                        base_url=cached["base_url"],
                    )
                except ValueError:
                    pass
                else:
                    self.session = cached
                    return

        self._session_discovery()

    def _session_discovery(self) -> None:
        """Performs session discovery by sending a GET request to the JMAP server's well-known URL and storing the session information."""

        try:
            response = self._http.get(self._well_known_url, headers={"Accept": "application/json"})
        except httpx.TransportError as e:
            raise MailServerUnavailableError() from e
        _raise_for_status(response)

        raw = response.json()
        # The post-redirect URL, which relative endpoint URLs in the session resolve against.
        base_url = str(response.url)
        self._session_obj = Session.from_wire(raw, base_url=base_url)
        self.session = {**raw, "timestamp": time.time(), "base_url": base_url}

        if self._session_manager:
            self._session_manager.set_session(self.session)

        self._rebind_client()

    def _rebind_client(self) -> None:
        """Points the materialized client (if any) at the freshly discovered session."""

        self._capabilities_cache.clear()

        client = self.__dict__.get("client")
        if client is not None:
            client.session = self._session_obj
            client.capabilities = self.capabilities_for(None)
            client.session_stale = False

    @cached_property
    def client(self) -> JMAPClient:
        """The jmaplib client bound to this connection's session and HTTP transport.

        Retries stay disabled (``max_attempts=1``): Suite mutations do not carry ``ifInState``
        guards yet, and retrying a maybe-applied submission would duplicate sends.
        """

        return JMAPClient(
            self._session_obj,
            self.capabilities_for(None),
            self._http,
            registry=RAW_REGISTRY,
            retry_policy=RetryPolicy(max_attempts=1),
            default_account=None,
            session_url=self._well_known_url,
        )

    def capabilities_for(self, account_id: str | None) -> ActiveCapabilities:
        """Resolves what the server supports for the given account (or session-wide for None).

        Always resolved with ``experimental=True``: the calendars capabilities Suite depends on
        track draft specs, which the registry otherwise leaves unresolved.
        """

        if account_id not in self._capabilities_cache:
            self._capabilities_cache[account_id] = RAW_REGISTRY.resolve(
                self._session_obj,
                Id(account_id) if account_id else None,
                experimental=True,
            )

        return self._capabilities_cache[account_id]

    def handle_session_drift(self) -> None:
        """Refreshes the session after a response reported a different ``sessionState``.

        Deliberately not ``client.refresh_session()``: that would resolve capabilities without
        ``experimental=True`` and bypass the persisted session, leaving Redis stale.

        The session state only changes when the set of accounts available to the user changes on
        the JMAP server, so the local JMAP Account documents are resynced against the fresh session.
        """

        self._session_discovery()

        if self.user:
            # Lazy import to avoid a circular dependency (jmap_account -> suite.mail.jmap -> client).
            from suite.mail.doctype.jmap_account.jmap_account import sync_jmap_accounts

            sync_jmap_accounts(self.user, self.accounts)

    @contextmanager
    def map_transport_errors(self):
        """Maps jmaplib transport failures and gateway errors to :class:`MailServerUnavailableError`."""

        try:
            yield
        except TransportError as e:
            raise MailServerUnavailableError() from e
        except RequestError as e:
            if e.status in UNAVAILABLE_STATUS_CODES:
                raise MailServerUnavailableError() from e
            raise

    @property
    def capabilities(self) -> dict:
        """Returns the capabilities of the JMAP server."""

        return self.session["capabilities"]

    @property
    def accounts(self) -> dict:
        """Returns the accounts for the logged-in user."""

        return self.session["accounts"]

    @property
    def primary_accounts(self) -> dict:
        """Returns the primary accounts for the logged-in user."""

        return self.session["primaryAccounts"]

    @property
    def api_url(self) -> str:
        """Returns the API URL of the JMAP server."""

        return self._session_obj.api_url

    @property
    def download_url(self) -> str:
        """Returns the download URL for the JMAP server."""

        return self._session_obj.download_url

    @property
    def upload_url(self) -> str:
        """Returns the upload URL for the JMAP server."""

        return self._session_obj.upload_url

    @property
    def event_source_url(self) -> str:
        """Returns the event source URL for the JMAP server."""

        return self._session_obj.event_source_url

    @property
    def state(self) -> str:
        """Returns the state of the JMAP server."""

        return self._session_obj.state

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        data: bytes | str | None = None,
        params: dict | None = None,
        timeout: float | tuple[float, float] | None = None,
        return_json: bool = True,
        **kwargs,
    ) -> dict | bytes:
        """Sends a request to the JMAP server with the specified parameters, and returns the response."""

        if timeout is None:
            timeout = httpx.USE_CLIENT_DEFAULT
        elif isinstance(timeout, tuple):
            timeout = httpx.Timeout(
                connect=timeout[0], read=timeout[1], write=timeout[1], pool=timeout[0]
            )

        try:
            response = self._http.request(
                method=method,
                url=url,
                headers=headers or {},
                json=json,
                content=data.encode("utf-8") if isinstance(data, str) else data,
                params=params,
                timeout=timeout,
                **kwargs,
            )
        except httpx.TransportError as e:
            raise MailServerUnavailableError() from e

        _raise_for_status(response)

        if return_json:
            return response.json()

        return response.content


def _raise_for_status(response: httpx.Response) -> None:
    """Raises an HTTPStatusError if the response status code indicates an error.

    Gateway errors (502/503/504) mean the JMAP server behind a reverse proxy is down, not that
    the request itself was bad, so they surface as MailServerUnavailableError instead.
    """

    if response.status_code in UNAVAILABLE_STATUS_CODES:
        raise MailServerUnavailableError() from httpx.HTTPStatusError(
            f"Request failed with status {response.status_code}: {response.text}",
            request=response.request,
            response=response,
        )

    if response.is_error:
        raise httpx.HTTPStatusError(
            f"Request failed with status {response.status_code}: {response.text}",
            request=response.request,
            response=response,
        )
