"""Contacts-domain helpers on top of jmaplib: address books and JSContact cards."""

from uuid import uuid7

from suite.mail.jmap.context import JMAPContext
from suite.utils.dt import utcnow

DEFAULT_CARD_PROPERTIES = [
    # --- JMAP-specific ---
    "id",
    "addressBookIds",
    "blobId",
    # --- JSContact core fields ---
    "uid",
    "kind",
    "prodId",
    "version",
    "created",
    "updated",
    "fullName",
    "name",
    "nickNames",
    "categories",
    "notes",
    "anniversaries",
    "urls",
    "relatedTo",
    "organizations",
    "titles",
    "roles",
    "emails",
    "phones",
    "addresses",
    "onlineServices",
    "preferredLanguages",
    "speakToAs",
    "gender",
    "timeZones",
    "photos",
    "members",
    "preferredContactChannels",
    "localizations",
    "extensions",
]


# -- address books ---------------------------------------------------------- #
def _address_book_payload(address_book: dict) -> dict:
    return {
        "name": address_book["name"],
        "description": address_book.get("description"),
        "sortOrder": int(address_book.get("sort_order") or 0),
        "isSubscribed": bool(address_book.get("is_subscribed") or False),
    }


def create_address_books(ctx: JMAPContext, address_books: list[dict]) -> dict:
    """Creates address books from simplified dicts, honoring is_default via onSuccessSetIsDefault."""

    payload = {}
    kwargs = {}
    for address_book in address_books:
        payload[address_book["creation_id"]] = _address_book_payload(address_book)

        if bool(address_book.get("is_default") or False):
            kwargs["onSuccessSetIsDefault"] = f"#{address_book['creation_id']}"

    return ctx.create("AddressBook", payload, **kwargs)


def update_address_books(ctx: JMAPContext, address_books: list[dict]) -> dict:
    """Updates address books from simplified dicts, honoring is_default via onSuccessSetIsDefault."""

    payload = {}
    kwargs = {}
    for address_book in address_books:
        payload[address_book["id"]] = _address_book_payload(address_book)

        if bool(address_book.get("is_default") or False):
            kwargs["onSuccessSetIsDefault"] = address_book["id"]

    return ctx.update("AddressBook", payload, **kwargs)


def delete_address_books(ctx: JMAPContext, ids: list[str], remove_contents: bool = False) -> dict:
    """Deletes address books by ids, optionally destroying the cards they contain."""

    return ctx.destroy("AddressBook", ids, onDestroyRemoveContents=remove_contents)


def get_default_address_book_id(ctx: JMAPContext, raise_exception: bool = False) -> str | None:
    """Returns the ID of the default address book, or None if not found."""

    for address_book in ctx.address_books:
        if address_book.get("isDefault"):
            return address_book["id"]

    if raise_exception:
        raise ValueError("No default address book found.")


# -- contact cards ---------------------------------------------------------- #
def get_contact_cards(
    ctx: JMAPContext, ids: list[str] | None = None, properties: list[str] | None = None
) -> list[dict]:
    """Gets contact cards by ids (or all of them), with the full JSContact property set by default."""

    return ctx.get_all("ContactCard", ids, properties=properties or DEFAULT_CARD_PROPERTIES)


def create_contact_cards(ctx: JMAPContext, contact_cards: list[dict]) -> dict:
    """Creates contact cards from simplified dicts (creation_id, full_name, emails, phones,
    addresses, address_book_ids, kind)."""

    payload = {}
    timestamp = utcnow()
    for contact_card in contact_cards:
        payload[contact_card["creation_id"]] = {
            "@type": "Card",
            "version": "1.0",
            "uid": contact_card["creation_id"],
            "kind": contact_card.get("kind", "individual"),
            "name": _get_name_map(contact_card.get("full_name")),
            "emails": _get_emails_map(contact_card.get("emails")),
            "phones": _get_phones_map(contact_card.get("phones")),
            "addresses": _get_addresses_map(contact_card.get("addresses")),
            "addressBookIds": {id: True for id in contact_card["address_book_ids"]},
            "created": timestamp,
            "updated": timestamp,
        }

    return ctx.create("ContactCard", payload)


def update_contact_cards(ctx: JMAPContext, contact_cards: list[dict]) -> dict:
    """Updates contact cards from simplified dicts (id, full_name, emails, phones, addresses,
    address_book_ids, kind)."""

    payload = {}
    for contact_card in contact_cards:
        payload[contact_card["id"]] = {
            "kind": contact_card.get("kind", "individual"),
            "name": _get_name_map(contact_card.get("full_name")),
            "emails": _get_emails_map(contact_card.get("emails")),
            "phones": _get_phones_map(contact_card.get("phones")),
            "addresses": _get_addresses_map(contact_card.get("addresses")),
            "addressBookIds": {id: True for id in contact_card["address_book_ids"]},
            "updated": utcnow(),
        }

    return ctx.update("ContactCard", payload)


def parse_contact_cards(ctx: JMAPContext, blob_ids: list[str]) -> dict:
    """Parses vCard blobs into JSContact Cards via 'ContactCard/parse'.

    The number of blob ids allowed per call is capped well below 'maxObjectsInGet' and is not
    advertised in the session capabilities, so we start from the general object limit and halve
    the batch whenever the server responds with 'requestTooLarge', reusing the largest size that
    succeeds for the remaining blobs. Any other error is raised so the caller can fall back to
    local parsing."""

    result = {"parsed": {}, "notFound": {}, "notParsable": {}}

    remaining = list(blob_ids)
    batch_size = min(len(remaining), ctx.limits.max_objects_in_get) or 1
    while remaining:
        batch = remaining[:batch_size]
        body = ctx.call("ContactCard/parse", {"blobIds": batch})

        if error := body.get("error"):
            if error.get("type") == "requestTooLarge" and batch_size > 1:
                batch_size = max(1, batch_size // 2)
                continue
            raise RuntimeError(f"ContactCard/parse failed: {error}")

        result["parsed"].update(body.get("parsed", {}))
        # The server reports notFound/notParsable as blob-id arrays; keep the
        # dict shape callers read (.keys()) by keying the ids.
        if not_found := body.get("notFound"):
            result["notFound"].update(dict.fromkeys(not_found) if isinstance(not_found, list) else not_found)
        if not_parsable := body.get("notParsable"):
            result["notParsable"].update(
                dict.fromkeys(not_parsable) if isinstance(not_parsable, list) else not_parsable
            )

        remaining = remaining[batch_size:]

    return result


def get_master_card_ids(ctx: JMAPContext, uids: list[str]) -> list[str]:
    """Gets contact card IDs for a list of UIDs.

    The UIDs are batched into separate OR queries so the filter stays within server limits, and
    each batch is fully paginated using the reported ``total``. We cannot rely on the generic
    query helper here: it stops as soon as a page returns fewer IDs than requested, but a server
    may cap a query page below that, which would silently drop matches after the first page and
    let a re-run create duplicate cards."""

    from suite.mail.jmap.context import create_batches

    if not uids:
        return []

    ids: list[str] = []
    for batch in create_batches(uids, ctx.limits.max_objects_in_get):
        filter = {"operator": "OR", "conditions": [{"uid": uid} for uid in batch]}
        position = 0
        total = None
        while True:
            query_response = ctx.call(
                "ContactCard/query",
                {
                    "filter": filter,
                    "position": position,
                    "limit": len(batch),
                    "calculateTotal": total is None,
                },
            )

            page = query_response.get("ids", [])
            ids.extend(page)

            if total is None:
                total = query_response.get("total")

            position += len(page)
            if not page or (total is not None and position >= total):
                break

    return ids


def update_card_address_book_ids(
    ctx: JMAPContext,
    ids: list[str],
    add_address_book_id: str | None = None,
    remove_address_book_id: str | None = None,
    move_to_address_book_id: str | None = None,
) -> dict:
    """
    Updates addressBookIds for the provided contact cards.

    Behavior:
    - add_address_book_id: adds the contact to an address book
    - remove_address_book_id: removes the contact from an address book
    - add + remove: moves contact between address books (patch-based)
    - move_to_address_book_id: replaces addressBookIds entirely
    """

    if move_to_address_book_id and (add_address_book_id or remove_address_book_id):
        raise ValueError(
            "Cannot specify 'move_to_address_book_id' together with 'add_address_book_id' or 'remove_address_book_id'."
        )

    if not any([add_address_book_id, remove_address_book_id, move_to_address_book_id]):
        raise ValueError(
            "At least one of 'add_address_book_id', 'remove_address_book_id', or 'move_to_address_book_id' must be specified."
        )

    if move_to_address_book_id:
        patch = {"addressBookIds": {move_to_address_book_id: True}, "updated": utcnow()}
    else:
        patch = {"updated": utcnow()}

        if add_address_book_id:
            patch[f"addressBookIds/{add_address_book_id}"] = True
        if remove_address_book_id:
            patch[f"addressBookIds/{remove_address_book_id}"] = None

    return ctx.update("ContactCard", {id: patch for id in ids})


def set_card_address_book_ids(ctx: JMAPContext, mapping: dict[str, dict[str, bool]]) -> dict:
    """Replaces the addressBookIds of each given card with the provided map.

    Used by the import rollback flow to move cards out of the staging address book into their
    final address book(s) once every card has been created; only addressBookIds is touched, so
    the rest of the card is left untouched — and the server manages the `updated` timestamp."""

    return ctx.update("ContactCard", {id: {"addressBookIds": book_ids} for id, book_ids in mapping.items()})


def _get_name_map(full_name: str | None = None) -> dict:
    """Helper function to construct the 'name' property map for a contact card based on the provided full name."""

    if full_name:
        given, surname = full_name.split(" ", 1) if " " in full_name else (full_name, None)
        return {
            "@type": "Name",
            "full": full_name,
            "components": [{"kind": "given", "value": given}, {"kind": "surname", "value": surname}],
            "isOrdered": True,
        }

    return {}


def _get_emails_map(emails: list[dict] | None = None) -> dict[str, dict] | None:
    """Helper function to construct the 'emails' property map for a contact card based on the provided list of email dictionaries."""

    if emails:
        emails_map = {}
        for email in emails:
            emails_map[str(uuid7())] = {
                "address": email["address"],
                "label": email.get("label"),
                "contexts": {email["type"]: True},
            }

        return emails_map


def _get_phones_map(phones: list[dict] | None = None) -> dict[str, dict] | None:
    """Helper function to construct the 'phones' property map for a contact card based on the provided list of phone dictionaries."""

    if phones:
        phones_map = {}
        for phone in phones:
            phones_map[str(uuid7())] = {
                "number": phone["number"],
                "label": phone.get("label"),
                "contexts": {phone["type"]: True},
            }

        return phones_map


def _get_addresses_map(addresses: list[dict] | None = None) -> dict[str, dict] | None:
    """Helper function to construct the 'addresses' property map for a contact card based on the provided list of address dictionaries."""

    if addresses:
        counter = 0
        addresses_map = {}
        for address in addresses:
            components = []
            for field, key in {
                "street": "name",
                "locality": "locality",
                "region": "region",
                "postcode": "postcode",
                "country": "country",
            }.items():
                components.append({"kind": key, "value": address.get(field)})

            addresses_map[f"{counter}"] = {
                "components": components,
                "timeZone": address.get("time_zone"),
                "contexts": {address["type"]: True},
            }
            counter += 1

        return addresses_map
