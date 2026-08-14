from typing import ClassVar

from suite.mail.jmap.services.mail.mail import MailService


class IdentityService(MailService):
    """Service for handling identity-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "Identity"
    capabilities: ClassVar[list[str]] = [
        "urn:ietf:params:jmap:core",
        "urn:ietf:params:jmap:mail",
        "urn:ietf:params:jmap:submission",
    ]

    def create(self, identities: list[dict]) -> dict:
        """Public method to create identities, handling batching if the number of identities exceeds the server's maximum allowed in a single 'set' call."""

        result = {"created": {}, "notCreated": {}}
        for batch in self.create_batches(identities, self.max_objects_in_set):
            payload = {}
            for identity in batch:
                payload[identity["creation_id"]] = {
                    "email": identity["email"],
                    "name": identity.get("name"),
                    "replyTo": identity.get("reply_to"),
                    "bcc": identity.get("bcc"),
                    "textSignature": identity.get("text_signature"),
                    "htmlSignature": identity.get("html_signature"),
                }

            body = self._create(payload)

            result["created"].update(body.get("created", {}))
            if not_created := body.get("notCreated", {}):
                result["notCreated"].update(not_created)

        return result

    def get(self, ids: list[str] | None = None) -> list[dict]:
        """Public method to get identities, handling batching if a list of ids is provided."""

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(self._get(batch).get("list", []))
        else:
            results.extend(self._get().get("list", []))

        return results

    def update(self, identities: list[dict]) -> dict:
        """Public method to update identities, handling batching if the number of identities exceeds the server's maximum allowed in a single 'set' call."""

        result = {"updated": [], "notUpdated": {}}
        for batch in self.create_batches(identities, self.max_objects_in_set):
            payload = {}
            for identity in batch:
                payload[identity["id"]] = {
                    "name": identity.get("name"),
                    "replyTo": identity.get("reply_to"),
                    "bcc": identity.get("bcc"),
                    "textSignature": identity.get("text_signature"),
                    "htmlSignature": identity.get("html_signature"),
                }

            body = self._update(payload)

            result["updated"].extend(body.get("updated", {}).keys())
            if not_updated := body.get("notUpdated", {}):
                result["notUpdated"].update(not_updated)

        return result

    def delete(self, ids: list[str]) -> dict:
        """Public method to delete identities, handling batching if the number of IDs exceeds the server's maximum allowed in a single 'set' call."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in self.create_batches(ids, self.max_objects_in_set):
            body = self._delete(batch)

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result

    def get_identity_id_by_email(self, email: str, raise_exception: bool = False) -> str | None:
        """Returns the identity ID for the given email, or raises an exception if not found and raise_exception is True."""

        for identity in self.identities:
            if identity["email"].lower() == email.lower():
                return identity["id"]

        if raise_exception:
            raise ValueError(f"No identity found for email: {email}")
