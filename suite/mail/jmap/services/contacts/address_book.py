from typing import ClassVar

from suite.mail.jmap.services.contacts.contacts import ContactsService


class AddressBookService(ContactsService):
    """Service for handling address book-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "AddressBook"

    def create(self, address_books: list[dict]) -> dict:
        """Public method to create address books, handling batching if the number of address books exceeds the server's maximum allowed in a single 'set' call."""

        result = {"created": {}, "notCreated": {}}
        for batch in self.create_batches(address_books, self.max_objects_in_set):
            payload = {}
            kwargs = {}
            for address_book in batch:
                payload[address_book["creation_id"]] = {
                    "name": address_book["name"],
                    "description": address_book.get("description"),
                    "sortOrder": int(address_book.get("sort_order") or 0),
                    "isSubscribed": bool(address_book.get("is_subscribed") or False),
                }

                if bool(address_book.get("is_default") or False):
                    kwargs["onSuccessSetIsDefault"] = f"#{address_book['creation_id']}"

            body = self._create(payload, **kwargs)

            result["created"].update(body.get("created", {}))
            if not_created := body.get("notCreated", {}):
                result["notCreated"].update(not_created)

        return result

    def get(self, ids: list[str] | None = None) -> list[dict]:
        """Public method to get address books, handling batching if a list of ids is provided."""

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(self._get(batch).get("list", []))
        else:
            results.extend(self._get().get("list", []))

        return results

    def update(self, address_books: list[dict]) -> dict:
        """Public method to update address books, handling batching if the number of address books exceeds the server's maximum allowed in a single 'set' call."""

        result = {"updated": [], "notUpdated": {}}
        for batch in self.create_batches(address_books, self.max_objects_in_set):
            payload = {}
            kwargs = {}
            for address_book in batch:
                payload[address_book["id"]] = {
                    "name": address_book["name"],
                    "description": address_book.get("description"),
                    "sortOrder": int(address_book.get("sort_order") or 0),
                    "isSubscribed": bool(address_book.get("is_subscribed") or False),
                }

                if bool(address_book.get("is_default") or False):
                    kwargs["onSuccessSetIsDefault"] = address_book["id"]

            body = self._update(payload, **kwargs)

            result["updated"].extend(body.get("updated", {}).keys())
            if not_updated := body.get("notUpdated", {}):
                result["notUpdated"].update(not_updated)

        return result

    def delete(self, ids: list[str], remove_contents: bool = False) -> dict:
        """Public method to delete address books, handling batching if the number of address book IDs exceeds the server's maximum allowed in a single 'set' call."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in self.create_batches(ids, self.max_objects_in_set):
            body = self._delete(batch, onDestroyRemoveContents=remove_contents)

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result

    def get_default(self, raise_exception: bool = False) -> str | None:
        """Returns the ID of the default address book, or None if no default address book is found. If raise_exception is True, raises a ValueError if no default address book is found."""

        for address_book in self.address_books:
            if address_book.get("isDefault"):
                return address_book["id"]

        if raise_exception:
            raise ValueError("No default address book found.")
