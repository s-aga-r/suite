from typing import ClassVar

from suite.mail.jmap.services.mail.mail import MailService


class ThreadService(MailService):
    """Service for handling thread-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "Thread"

    def get(self, ids: list[str] | None = None) -> dict[str, list]:
        """Public method to get threads, handling batching if a list of ids is provided."""

        result = {}
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                if threads := self._get(batch, properties=["emailIds"]).get("list", []):
                    result.update({thread["id"]: thread["emailIds"] for thread in threads})
        else:
            if threads := self._get(properties=["emailIds"]).get("list", []):
                result.update({thread["id"]: thread["emailIds"] for thread in threads})

        return result
