"""Mail-domain helpers on top of jmaplib: emails, mailboxes, identities, and submissions.

Every function takes a :class:`~suite.mail.jmap.context.JMAPContext` and speaks JMAP through
jmaplib batches; payload shaping and the compose flow live here so call sites stay declarative.
"""

from typing import Literal
from uuid import uuid7

import frappe
from jmap.batch import Batch
from jmap.core.ids import CreationRef
from jmap.core.invocation import Handle

from suite import __version__
from suite.mail.jmap.context import JMAPContext
from suite.mail.jmap.models import EmailAttachment, EmailCreateModel, EmailRecipient
from suite.mail.utils.dt import to_utc_z

DEFAULT_EMAIL_PROPERTIES = [
    "id",
    "blobId",
    "threadId",
    "mailboxIds",
    "keywords",
    "size",
    "receivedAt",
    "sentAt",
    "hasAttachment",
    "subject",
    "preview",
    "from",
    "to",
    "cc",
    "bcc",
    "replyTo",
    "sender",
    "messageId",
    "inReplyTo",
    "references",
    "htmlBody",
    "textBody",
    "bodyValues",
    "attachments",
]

SUBMISSION_PROPERTIES = ["id", "emailId", "undoStatus", "sendAt"]


# -- emails ----------------------------------------------------------------- #
def get_emails(ctx: JMAPContext, ids: list[str], properties: list[str] | None = None) -> list[dict]:
    """Gets emails by ids (or all emails when empty), with full body values."""

    return ctx.get_all(
        "Email", ids, properties=properties or DEFAULT_EMAIL_PROPERTIES, fetchAllBodyValues=True
    )


def update_emails(
    ctx: JMAPContext, emails: list[dict], replace_keywords: bool = False, replace_mailboxes: bool = False
) -> dict:
    """Updates emails' keywords and mailboxIds — as patches by default, or whole-property
    replacements when the replace flags are set."""

    payload = {}
    for email in emails:
        payload[email["id"]] = {}

        if keywords := email.get("keywords", {}):
            if replace_keywords:
                payload[email["id"]]["keywords"] = keywords
            else:
                payload[email["id"]].update({f"keywords/{k}": v for k, v in keywords.items()})

        if mailbox_ids := email.get("mailbox_ids", {}):
            if replace_mailboxes:
                payload[email["id"]]["mailboxIds"] = mailbox_ids
            else:
                payload[email["id"]].update({f"mailboxIds/{k}": v for k, v in mailbox_ids.items()})

        if not payload[email["id"]]:
            raise ValueError("At least one of 'keywords' or 'mailbox_ids' must be provided for update.")

    return ctx.update("Email", payload)


def delete_emails(ctx: JMAPContext, ids: list[str]) -> dict:
    """Deletes emails by ids."""

    return ctx.destroy("Email", ids)


def query_emails(
    ctx: JMAPContext,
    filter: dict | None = None,
    position: int = 0,
    limit: int = 50,
    sort: list[dict] | None = None,
) -> dict:
    """Queries emails, newest first by default, returning `{"ids", "total"}`."""

    return ctx.query(
        "Email", filter, position, limit, sort or [{"property": "receivedAt", "isAscending": False}]
    )


def query_email_threads(
    ctx: JMAPContext, filter: dict | None = None, position: int = 0, limit: int = 50, fetch_all: bool = False
) -> list[str] | dict[str, list[str]]:
    """Queries email threads, returning either the matching email ids or a mapping of thread ids
    to their email ids, resolved in one request via back-references."""

    batch = ctx.batch()
    query = batch.add(
        "Email/query",
        {
            "filter": filter or {},
            "sort": [{"property": "receivedAt", "isAscending": False}],
            "collapseThreads": True,
            "position": position,
            "limit": limit,
        },
    )

    threads = None
    if fetch_all:
        emails = batch.add("Email/get", {"ids": query.ref_ids(), "properties": ["threadId"]})
        threads = batch.add(
            "Thread/get", {"ids": emails.ref_list("threadId"), "properties": ["id", "emailIds"]}
        )

    ctx.run(batch)

    if not fetch_all:
        return ctx.read(query).get("ids", [])

    return {thread["id"]: thread.get("emailIds", []) for thread in ctx.read(threads).get("list", [])}


def get_threads(ctx: JMAPContext, ids: list[str] | None = None) -> dict[str, list]:
    """Gets threads by ids, returning a mapping of thread ids to their email ids."""

    return {thread["id"]: thread["emailIds"] for thread in ctx.get_all("Thread", ids, properties=["emailIds"])}


def search_emails(ctx: JMAPContext, text: str, limit: int = 50, separate_requests: bool = False) -> list[str]:
    """Searches for emails matching the given text in subject, to, cc, bcc, body or text."""

    filters = [
        {"subject": text},
        {"to": text},
        {"cc": text},
        {"bcc": text},
        {"body": text},
        {"text": text},
    ]

    return _query_ids(ctx, filters, limit, separate_requests)


def get_email_suggestions(
    ctx: JMAPContext, text: str, limit: int = 5, separate_requests: bool = False
) -> list[str]:
    """Returns email addresses matching the given text across from/to/cc/bcc of recent emails."""

    addresses: list[str] = []

    filters = [
        {"from": text},
        {"to": text},
        {"cc": text},
        {"bcc": text},
    ]

    ids = _query_ids(ctx, filters, limit, separate_requests)
    if not ids:
        return addresses

    for email in get_emails(ctx, ids, properties=["from", "to", "cc", "bcc"]):
        for field in ("from", "to", "cc", "bcc"):
            for addr in email.get(field) or []:
                email_address = addr.get("email")
                if email_address and text.lower() in email_address.lower() and email_address not in addresses:
                    addresses.append(email_address)

    return addresses[:limit]


def _query_ids(ctx: JMAPContext, filters: list[dict], limit: int, separate_requests: bool = False) -> list[str]:
    """Runs one Email/query per filter (in one request, or one request each) and collects the
    unique email ids across every result, capped at the limit."""

    ids: list[str] = []

    def collect_ids(handles: list[Handle]) -> None:
        for handle in handles:
            for id in ctx.read(handle).get("ids", []):
                if id not in ids:
                    ids.append(id)

    def query_args(filter: dict) -> dict:
        return {
            "filter": filter,
            "position": 0,
            "limit": limit,
            "sort": [{"property": "receivedAt", "isAscending": False}],
            "calculateTotal": False,
        }

    if separate_requests:
        for filter in filters:
            batch = ctx.batch()
            handle = batch.add("Email/query", query_args(filter))
            ctx.run(batch)
            collect_ids([handle])
    else:
        batch = ctx.batch()
        handles = [batch.add("Email/query", query_args(filter)) for filter in filters]
        ctx.run(batch)
        collect_ids(handles)

    return ids[:limit]


# -- compose ---------------------------------------------------------------- #
def create_emails(ctx: JMAPContext, emails: list[EmailCreateModel]) -> dict:
    """Creates email drafts and optionally submits them in one JMAP request.

    Returns the raw response envelope; method responses arrive in queue order (drafts first, then
    the submission, then any implicit responses), which Mail Queue reads positionally and persists
    as JSON.
    """

    batch = ctx.batch()
    handles, draft_refs = _queue_drafts(ctx, batch, emails)
    handles.extend(_queue_submissions(ctx, batch, emails, draft_refs))

    ctx.run(batch)

    return _to_envelope(handles)


def _to_envelope(handles: list[Handle]) -> dict:
    """Reconstructs the raw `methodResponses` envelope from resolved handles, keeping each call's
    implicit responses (e.g. the Email/set emitted by onSuccessUpdateEmail) right after it, so
    positional consumers keep working."""

    method_responses = []
    for handle in handles:
        if handle.error is not None:
            method_responses.append(["error", handle.error.arguments, handle.call_id])
        else:
            method_responses.append([handle.call.name, handle.result, handle.call_id])

        for extra in handle.extra:
            method_responses.append([extra.name, extra.arguments, extra.method_call_id])

    return {"methodResponses": method_responses}


def _queue_drafts(
    ctx: JMAPContext, batch: Batch, emails: list[EmailCreateModel]
) -> tuple[list[Handle], dict]:
    """Queues draft creation for the given emails on the batch, returning the handles along with
    a mapping of creation ids to draft references."""

    handles = []
    draft_refs = {}

    draft_mailbox_id = get_mailbox_id_by_role(ctx, "drafts", create_if_not_exists=True, raise_exception=True)

    for email in emails:
        draft_ref = f"draft-{email.creation_id}"
        draft_refs[email.creation_id] = draft_ref

        # --------------------------------------------------
        # RAW MESSAGE → Email/import
        # --------------------------------------------------

        if email.raw_message:
            blob = ctx.upload_blob(email.raw_message.encode("utf-8"), content_type="message/rfc822")

            handles.append(
                batch.add(
                    "Email/import",
                    {
                        "emails": {
                            draft_ref: {
                                "blobId": blob["blobId"],
                                "mailboxIds": {draft_mailbox_id: True},
                                "keywords": {"$draft": True, "$seen": True},
                            }
                        },
                    },
                )
            )

            # Destroy old email if existing_id is provided.
            if email.existing_id:
                handles.append(batch.add("Email/set", {"destroy": [email.existing_id]}))

        # --------------------------------------------------
        # NORMAL DRAFT → Email/set
        # --------------------------------------------------

        else:
            payload = {"create": {draft_ref: _get_draft(email, draft_mailbox_id)}}

            if email.existing_id:
                payload["destroy"] = [email.existing_id]

            handles.append(batch.add("Email/set", payload))

    return handles, draft_refs


def _queue_submissions(
    ctx: JMAPContext, batch: Batch, emails: list[EmailCreateModel], draft_refs: dict[str, str]
) -> list[Handle]:
    """Queues email submissions for the given drafts on the shared batch and returns their handles."""

    draft_mailbox_id = get_mailbox_id_by_role(ctx, "drafts", create_if_not_exists=True, raise_exception=True)
    sent_mailbox_id = get_mailbox_id_by_role(ctx, "sent", create_if_not_exists=True, raise_exception=True)

    create_payload = {}
    on_success_update = {}
    on_success_destroy = []

    for email in emails:
        if email.save_as_draft:
            continue

        # -----------------------------
        # Get Identity
        # -----------------------------

        identity_id = get_identity_id_by_email(ctx, email.from_email, raise_exception=True)

        # -----------------------------
        # CREATE Submission
        # -----------------------------

        draft_ref = draft_refs[email.creation_id]
        submit_ref = f"submit-{email.creation_id}"

        create_payload[submit_ref] = {
            "identityId": identity_id,
            "emailId": CreationRef(draft_ref),
            "envelope": _build_envelope(
                from_email=email.from_email,
                rcpt_emails={r.email for r in email.recipients},
                envelope_id=email.creation_id,
                priority=email.priority,
                hold_until=email.hold_until,
            ),
        }

        # -----------------------------
        # Success Handlers
        # -----------------------------

        if email.destroy_after_submit:
            # No Mailbox updates, just destroy the draft email after successful submission.
            on_success_destroy.append(f"#{submit_ref}")

        else:
            # Move the draft email to the Sent mailbox and update keywords after successful submission.
            on_success_update[f"#{submit_ref}"] = {
                f"mailboxIds/{draft_mailbox_id}": None,
                f"mailboxIds/{sent_mailbox_id}": True,
                "keywords/$draft": None,
                "keywords/$seen": True,
            }

        # -----------------------------
        # Forward / Reply Keywords
        # -----------------------------

        for target_id, keyword in [
            (email.forwarded_id, "$forwarded"),
            (email.reply_to_id, "$answered"),
        ]:
            if target_id:
                on_success_update.setdefault(target_id, {})[f"keywords/{keyword}"] = True

    if not create_payload:
        return []

    args = {"create": create_payload}

    if on_success_update:
        args["onSuccessUpdateEmail"] = on_success_update

    if on_success_destroy:
        args["onSuccessDestroyEmail"] = on_success_destroy

    return [batch.add("EmailSubmission/set", args)]


def _get_recipients(
    recipients: list[EmailRecipient], kind: Literal["to", "cc", "bcc"]
) -> list[dict[str, str | None]]:
    """Helper function to filter recipients by type and format them for the JMAP payload."""

    return [{"name": r.name, "email": r.email} for r in recipients if r.type == kind]


def _get_draft(email: EmailCreateModel, draft_mailbox_id: str) -> dict:
    """Helper function to build the draft payload for the email creation."""

    draft = {
        "mailboxIds": {draft_mailbox_id: True},
        "keywords": {"$draft": True, "$seen": True},
        "from": [{"name": email.from_name, "email": email.from_email}],
    }

    # Add TO/CC/BCC
    if email.recipients:
        for kind in ("to", "cc", "bcc"):
            if rcpts := _get_recipients(email.recipients, kind):
                draft[kind] = rcpts

    if email.subject:
        draft["subject"] = email.subject

    # Headers
    if email.sent_at:
        # Mail Queue's sent_at holds system time; Stalwart wants the UTC ``...Z`` form.
        draft["sentAt"] = to_utc_z(email.sent_at)
    if email.message_id:
        draft["header:Message-ID"] = f"<{email.message_id}>"

    draft.update(
        {
            "header:User-Agent": f"Frappe Mail v{__version__} (Frappe v{frappe.__version__})",
            "header:X-Mailer": "Frappe Mail",
            "header:X-Mail-Queue": str(email.creation_id),
        }
    )

    if email.reply_to:
        draft["header:Reply-To"] = ", ".join(f'"{r.name}" <{r.email}>' for r in email.reply_to)

    if email.in_reply_to:
        draft["header:In-Reply-To"] = f"<{email.in_reply_to}>"

    if email.headers:
        for header in email.headers:
            draft[f"header:{header.name}"] = header.value

    # Body parts
    draft["bodyValues"] = {}

    text_part = html_part = None

    if email.text_body:
        text_part = {"partId": "text", "type": "text/plain"}
        draft["bodyValues"]["text"] = {
            "value": email.text_body,
            "charset": "utf-8",
            "isTruncated": False,
        }

    if email.html_body:
        html_part = {"partId": "html", "type": "text/html"}
        draft["bodyValues"]["html"] = {
            "value": email.html_body,
            "charset": "utf-8",
            "isTruncated": False,
        }

    # Attachments
    attachments = email.attachments or []
    inline_attachments = [a for a in attachments if a.disposition == "inline"]
    regular_attachments = [a for a in attachments if a.disposition != "inline"]
    body_parts = [p for p in (text_part, html_part) if p]

    if inline_attachments and body_parts:
        # Inline images are referenced from the HTML body via `cid:` URLs. Build an
        # explicit MIME structure that nests them inside a `multipart/related` container
        # (next to the body) instead of letting them become plain siblings of the body in
        # `multipart/mixed`. Some providers (e.g. AWS) treat every `multipart/mixed` part
        # as a regular attachment and reject inline images by extension, whereas clients
        # like Gmail wrap them in `multipart/related` so they are recognized as inline.
        body_root = (
            {"type": "multipart/alternative", "subParts": body_parts}
            if len(body_parts) > 1
            else body_parts[0]
        )

        body_structure = {
            "type": "multipart/related",
            "subParts": [body_root, *(_get_body_part(a) for a in inline_attachments)],
        }

        if regular_attachments:
            body_structure = {
                "type": "multipart/mixed",
                "subParts": [
                    body_structure,
                    *(_get_body_part(a) for a in regular_attachments),
                ],
            }

        draft["bodyStructure"] = body_structure
    else:
        # No inline images: let the server assemble the structure from the convenience
        # properties (`multipart/alternative` for the body, `multipart/mixed` for attachments).
        if text_part:
            draft["textBody"] = [text_part]
        if html_part:
            draft["htmlBody"] = [html_part]
        if attachments:
            draft["attachments"] = [_get_body_part(a) for a in attachments]

    return draft


def _get_body_part(attachment: EmailAttachment) -> dict[str, str]:
    """Helper function to build an EmailBodyPart payload for an attachment."""

    return {
        "name": attachment.name,
        "type": attachment.type,
        "cid": attachment.cid,
        "blobId": attachment.blob_id,
        "disposition": attachment.disposition,
    }


# -- submissions ------------------------------------------------------------ #
def get_submissions(ctx: JMAPContext, ids: list[str], properties: list[str] | None = None) -> list[dict]:
    """Gets email submissions by ids."""

    return ctx.get_all("EmailSubmission", ids, properties=properties or SUBMISSION_PROPERTIES)


def cancel_submission(ctx: JMAPContext, submission_id: str) -> None:
    """Cancels a held (FUTURERELEASE) submission by setting its undoStatus to 'canceled' — the
    only mutable property per RFC 8621 §7.5."""

    from suite.mail.jmap.context import get_jmap_set_error_message

    result = ctx.call("EmailSubmission/set", {"update": {submission_id: {"undoStatus": "canceled"}}})

    if submission_id not in (result.get("updated") or {}):
        raise ValueError(get_jmap_set_error_message(result, "notUpdated", submission_id))


def resubmit_submission(
    ctx: JMAPContext,
    email_id: str,
    from_email: str,
    rcpt_emails: list[str],
    envelope_id: str,
    priority: int = 0,
    hold_until: int | None = None,
) -> dict:
    """Creates a new submission for an already-stored email (reschedule / send-now: the old
    submission must be canceled first, since undoStatus is the only mutable property).

    Returns the created object; its echoed undoStatus is unreliable (Stalwart echoes "final"
    for held submissions) — use `get_submissions` for the real state.
    """

    from suite.mail.jmap.context import get_jmap_set_error_message

    identity_id = get_identity_id_by_email(ctx, from_email, raise_exception=True)

    submit_ref = f"submit-{envelope_id}"
    result = ctx.call(
        "EmailSubmission/set",
        {
            "create": {
                submit_ref: {
                    "identityId": identity_id,
                    "emailId": email_id,
                    "envelope": _build_envelope(from_email, rcpt_emails, envelope_id, priority, hold_until),
                }
            }
        },
    )

    created = (result.get("created") or {}).get(submit_ref)
    if not created:
        raise ValueError(get_jmap_set_error_message(result, "notCreated", submit_ref))

    return created


def max_delayed_send(ctx: JMAPContext) -> int:
    """Returns the maximum delay in seconds allowed for a FUTURERELEASE (RFC 4865) submission,
    defaulting to 30 days."""

    account = ctx.connection.accounts.get(ctx.account) or {}
    submission_caps = (account.get("accountCapabilities") or {}).get("urn:ietf:params:jmap:submission") or {}

    return int(submission_caps.get("maxDelayedSend") or 2_592_000)


def _build_envelope(
    from_email: str,
    rcpt_emails: set[str] | list[str],
    envelope_id: str,
    priority: int,
    hold_until: int | None = None,
) -> dict:
    """Builds the SMTP envelope for a submission; `hold_until` (epoch seconds) adds the RFC 4865
    HOLDUNTIL parameter so the server holds delivery."""

    parameters = {
        "RET": "FULL",
        "ENVID": envelope_id,
        "MT-PRIORITY": str(priority),
    }

    if hold_until:
        parameters["HOLDUNTIL"] = str(hold_until)

    return {
        "mailFrom": {
            "email": from_email,
            "parameters": parameters,
        },
        "rcptTo": [
            {
                "email": rcpt,
                "parameters": {
                    "NOTIFY": "DELAY,FAILURE",
                    "ORCPT": f"rfc822;{rcpt}",
                },
            }
            for rcpt in sorted(set(rcpt_emails))
        ],
    }


# -- identities ------------------------------------------------------------- #
def get_identity_id_by_email(ctx: JMAPContext, email: str, raise_exception: bool = False) -> str | None:
    """Returns the identity ID for the given email, or raises if not found and raise_exception is True."""

    for identity in ctx.identities:
        if identity["email"].lower() == email.lower():
            return identity["id"]

    if raise_exception:
        raise ValueError(f"No identity found for email: {email}")


# -- mailboxes -------------------------------------------------------------- #
def create_mailboxes(ctx: JMAPContext, mailboxes: list[dict]) -> dict:
    """Creates mailboxes from simplified dicts (creation_id, name, role, parent_id, sort_order,
    is_subscribed)."""

    payload = {
        mailbox["creation_id"]: {
            "name": mailbox["name"],
            "role": mailbox.get("role") or None,
            "parentId": mailbox.get("parent_id") or None,
            "sortOrder": int(mailbox.get("sort_order") or 0),
            "isSubscribed": bool(mailbox.get("is_subscribed") or False),
        }
        for mailbox in mailboxes
    }

    return ctx.create("Mailbox", payload)


def update_mailboxes(ctx: JMAPContext, mailboxes: list[dict]) -> dict:
    """Updates mailboxes from simplified dicts (id, name, role, parent_id, sort_order, is_subscribed)."""

    payload = {
        mailbox["id"]: {
            "name": mailbox["name"],
            "role": mailbox.get("role") or None,
            "parentId": mailbox.get("parent_id") or None,
            "sortOrder": int(mailbox.get("sort_order") or 0),
            "isSubscribed": bool(mailbox.get("is_subscribed") or False),
        }
        for mailbox in mailboxes
    }

    return ctx.update("Mailbox", payload)


def delete_mailboxes(ctx: JMAPContext, ids: list[str], remove_emails: bool = False) -> dict:
    """Deletes mailboxes by ids, optionally destroying the emails they contain."""

    return ctx.destroy("Mailbox", ids, onDestroyRemoveEmails=remove_emails)


def get_mailbox_id_by_role(
    ctx: JMAPContext, role: str, create_if_not_exists: bool = False, raise_exception: bool = False
) -> str | None:
    """Returns the mailbox ID for a given role, optionally creating it if missing."""

    def find_id(role: str) -> str | None:
        role = role.lower()
        for mailbox in ctx.mailboxes:
            if (mailbox.get("role") or "").lower() == role:
                return mailbox["id"]

    if mailbox_id := find_id(role):
        return mailbox_id

    if not create_if_not_exists:
        if raise_exception:
            raise ValueError(f"No mailbox found with role '{role}'")

        return None

    mailbox = {
        "creation_id": str(uuid7()),
        "name": role.title(),
        "role": role,
        "is_subscribed": True,
    }
    response = create_mailboxes(ctx, [mailbox])

    if response.get("notCreated") and raise_exception:
        raise ValueError(f"Failed to create mailbox with role '{role}'")

    ctx.invalidate_cache(ctx.account, key="mailboxes")

    return find_id(role)


def get_mailbox_role_by_id(ctx: JMAPContext, id: str, raise_exception: bool = False) -> str | None:
    """Returns the mailbox role for the given ID."""

    for mailbox in ctx.mailboxes:
        if mailbox["id"] == id:
            return mailbox["role"]

    if raise_exception:
        raise ValueError(f"No mailbox found with ID '{id}'")


def get_mailbox_name_by_id(ctx: JMAPContext, id: str, raise_exception: bool = False) -> str | None:
    """Returns the mailbox name for the given ID."""

    for mailbox in ctx.mailboxes:
        if id and mailbox["id"] == id:
            return mailbox["name"]

    if raise_exception:
        raise ValueError(f"No mailbox found with ID '{id}'")


def get_mailbox_id_by_name(ctx: JMAPContext, name: str, raise_exception: bool = False) -> str | None:
    """Returns the mailbox ID for the given name."""

    for mailbox in ctx.mailboxes:
        if name and mailbox["name"] == name:
            return mailbox["id"]

    if raise_exception:
        raise ValueError(f"No mailbox found with name '{name}'")
