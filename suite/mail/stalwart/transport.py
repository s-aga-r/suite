"""The requests-based JMAP transport used only by the Stalwart admin (management) client.

The generic mail/calendar client rides jmaplib (see suite.mail.jmap); this module survives for
the admin realm — vendor-URN management sessions, master-user auth, and raw /api endpoints —
until that migrates too. The shared connection dataclasses and the 503 error contract live in
suite.mail.jmap.client so both realms raise one class.
"""

import time
from urllib.parse import urljoin

import requests

from suite.mail.jmap.client import (
    UNAVAILABLE_STATUS_CODES,
    JMAPConnectionInfo,
    JMAPSessionManager,
    MailServerUnavailableError,
)


class JMAPConnection:
    """Manages the connection to a JMAP server, including discovery of server capabilities and sending requests."""

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

        self.__info = info
        self.__session = requests.Session()
        self.__session.auth = (self.__info.username, self.__info.password)
        self.__session.verify = self.__info.verify_ssl
        self.__session_manager = session_manager
        self.user = user

        self._initialize_session()

    def _initialize_session(self) -> dict:
        """Initializes the session by attempting to retrieve it from the session manager or performing session discovery."""

        if self.__session_manager:
            session = self.__session_manager.get_session()
            if session:
                self.session = session
                return

        self._session_discovery()

    def _session_discovery(self) -> None:
        """Performs session discovery by sending a GET request to the JMAP server's well-known URL and storing the session information."""

        url = urljoin(self.__info.url, "/.well-known/jmap")
        try:
            response = self.__session.get(
                url, headers={"Accept": "application/json"}, timeout=self.__info.timeout
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise MailServerUnavailableError() from e
        raise_for_status(response)

        self.session = response.json()
        self.session["timestamp"] = time.time()

        if self.__session_manager:
            self.__session_manager.set_session(self.session)

    @property
    def capabilities(self) -> dict:
        """Returns the capabilities of the JMAP server."""

        return self.session["capabilities"]

    @property
    def api_url(self) -> str:
        """Returns the API URL of the JMAP server."""

        return self.session["apiUrl"]

    @property
    def accounts(self) -> dict:
        """Returns the accounts for the logged-in user."""

        return self.session["accounts"]

    @property
    def primary_accounts(self) -> dict:
        """Returns the primary accounts for the logged-in user."""

        return self.session["primaryAccounts"]

    @property
    def download_url(self) -> str:
        """Returns the download URL for the JMAP server."""

        return self.session["downloadUrl"]

    @property
    def upload_url(self) -> str:
        """Returns the upload URL for the JMAP server."""

        return self.session["uploadUrl"]

    @property
    def event_source_url(self) -> str:
        """Returns the event source URL for the JMAP server."""

        return self.session["eventSourceUrl"]

    @property
    def state(self) -> str:
        """Returns the state of the JMAP server."""

        return self.session["state"]

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        data: bytes | str | None = None,
        params: dict | None = None,
        timeout: float | None = None,
        return_json: bool = True,
        **kwargs,
    ) -> dict | bytes:
        """Sends a request to the JMAP server with the specified parameters, and returns the response."""

        headers = headers or {}
        try:
            response = self.__session.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                data=data,
                params=params,
                timeout=timeout or self.__info.timeout,
                **kwargs,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise MailServerUnavailableError() from e

        raise_for_status(response)

        if return_json:
            return response.json()

        return response.content


def raise_for_status(response: requests.Response) -> None:
    """Raises an HTTPError if the response status code indicates an error.

    Gateway errors (502/503/504) mean the JMAP server behind a reverse proxy is down, not that
    the request itself was bad, so they surface as MailServerUnavailableError instead.
    """

    if response.status_code in UNAVAILABLE_STATUS_CODES:
        raise MailServerUnavailableError() from requests.exceptions.HTTPError(
            f"Request failed with status {response.status_code}: {response.text}", response=response
        )

    if not response.ok:
        raise requests.exceptions.HTTPError(
            f"Request failed with status {response.status_code}: {response.text}", response=response
        )
