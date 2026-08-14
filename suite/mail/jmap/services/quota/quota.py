from typing import ClassVar

from suite.mail.jmap.services.core import CoreService


class QuotaService(CoreService):
    """Service for handling quota-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "Quota"
    capabilities: ClassVar[list[str]] = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:quota"]

    def __post_init__(self) -> None:
        """Post-initialization to check if the JMAP server supports the Quota capability and raise an error if not."""

        super().__post_init__()

        if "urn:ietf:params:jmap:quota" not in self.connection.capabilities:
            raise NotImplementedError("The JMAP server does not support the Quota capability.")

    @property
    def primary_account_id(self) -> str:
        """Returns the primary account ID for the logged-in user."""

        return self.connection.primary_accounts["urn:ietf:params:jmap:quota"]

    def get(self, ids: list[str] | None = None) -> list[dict]:
        """Public method to get quotas, handling batching if a list of ids is provided."""

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(self._get(batch).get("list", []))
        else:
            results.extend(self._get().get("list", []))

        return results

    def query(
        self, filter: dict | None = None, position: int = 0, limit: int = 50, sort: list[dict] | None = None
    ) -> dict:
        """Public method to query quotas, handling batching if the number of results exceeds the server's maximum allowed in a single 'query' call."""

        ids = []
        total = None
        batch_size = min(limit, self.max_objects_in_get)

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
        """Public method to get quota changes since a given state."""

        return self._changes(since_state)
