from typing import ClassVar

from suite.mail.jmap.client import JMAPConnection
from suite.mail.jmap.services.core import CoreService


class PushSubscriptionService(CoreService):
    """Service for handling push subscription-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "PushSubscription"
    # PushSubscription requests are user-scoped and carry no accountId.
    account_scoped: ClassVar[bool] = False

    def __init__(self, connection: JMAPConnection) -> None:
        """Initializes the PushSubscriptionService with the provided JMAP connection."""

        self.connection = connection
        # PushSubscription is user-scoped, not account-scoped (requests omit accountId); key the
        # cache by the connection's user so subscriptions for different users don't collide.
        self.account = connection.user

    def create(self, subscriptions: list[dict]) -> dict:
        """Public method to create push subscriptions, handling batching if the number of subscriptions exceeds the server's maximum allowed in a single 'set' call."""

        result = {"created": {}, "notCreated": {}}
        for batch in self.create_batches(subscriptions, self.max_objects_in_set):
            payload = {}
            for subscription in batch:
                payload[subscription["creation_id"]] = {
                    "deviceClientId": subscription["device_client_id"],
                    "url": subscription["url"],
                    "keys": subscription.get("keys") or None,
                    "types": subscription["types"],
                }

            body = self._create(payload)

            result["created"].update(body.get("created", {}))
            if not_created := body.get("notCreated", {}):
                result["notCreated"].update(not_created)

        return result

    def get(self, ids: list[str] | None = None) -> list[dict]:
        """Public method to get push subscriptions, handling batching if a list of ids is provided."""

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(self._get(batch).get("list", []))
        else:
            results.extend(self._get().get("list", []))

        return results

    def update(self, subscriptions: list[dict]) -> dict:
        """Public method to update push subscriptions, handling batching if the number of subscriptions exceeds the server's maximum allowed in a single 'set' call."""

        result = {"updated": [], "notUpdated": {}}
        for batch in self.create_batches(subscriptions, self.max_objects_in_set):
            payload = {}
            for subscription in batch:
                patch = {}

                if verification_code := subscription.get("verification_code"):
                    patch["verificationCode"] = verification_code
                if "types" in subscription:
                    patch["types"] = subscription["types"]
                # A bare {"id": ...} is a renewal: expires=null makes the server bump the
                # subscription to its maximum lifetime.
                if not patch or "expires" in subscription:
                    patch["expires"] = subscription.get("expires")

                payload[subscription["id"]] = patch

            body = self._update(payload)

            result["updated"].extend(body.get("updated", {}).keys())
            if not_updated := body.get("notUpdated", {}):
                result["notUpdated"].update(not_updated)

        return result

    def delete(self, ids: list[str]) -> dict:
        """Public method to delete push subscriptions, handling batching if the number of IDs exceeds the server's maximum allowed in a single 'set' call."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in self.create_batches(ids, self.max_objects_in_set):
            body = self._delete(batch)

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result
