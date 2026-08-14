from typing import ClassVar

from suite.mail.jmap.models import UploadObject
from suite.mail.jmap.services.core import CoreService


class BlobService(CoreService):
    """Service for handling blob-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "Blob"

    @property
    def primary_account_id(self) -> str:
        """Returns the primary account ID for the logged-in user."""

        return self.connection.primary_accounts["urn:ietf:params:jmap:blob"]

    def upload(self, blobs: dict[str, UploadObject]) -> dict:
        """Public method to upload blobs, handling batching if the number of blobs exceeds the server's maximum allowed in a single 'set' call."""

        result = {"created": {}, "notCreated": {}}
        for batch in self.batch_dict(blobs, self.max_objects_in_set):
            payload = {creation_id: upload.to_json() for creation_id, upload in batch.items()}
            body = self._call_one(f"{self._type}/upload", {"create": payload})

            result["created"].update(body.get("created", {}))
            if not_created := body.get("notCreated", {}):
                result["notCreated"].update(not_created)

        return result

    def get(self, ids: list[str], properties: list[str]) -> list[dict]:
        """Public method to retrieve blobs by their IDs, handling batching if the number of IDs exceeds the server's maximum allowed in a single 'get' call."""

        results = []
        for batch in self.create_batches(ids, self.max_objects_in_get):
            results.extend(self._get(batch, properties=properties).get("list", []))

        return results

    def lookup(self, ids: list[str], type_names: list[str]) -> list[dict]:
        """Public method to look up blobs by their IDs and type names, handling batching if the number of IDs exceeds the server's maximum allowed in a single 'lookup' call."""

        results = []
        for batch in self.create_batches(ids, self.max_objects_in_get):
            results.extend(
                self._call_one(f"{self._type}/lookup", {"ids": batch, "typeNames": type_names}).get(
                    "list", []
                )
            )

        return results
