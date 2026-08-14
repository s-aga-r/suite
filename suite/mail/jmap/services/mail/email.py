from typing import ClassVar, Literal

import frappe
from jmap.batch import Batch
from jmap.core.invocation import Handle

from suite import __version__
from suite.mail.jmap.models import EmailAttachment, EmailCreateModel, EmailRecipient
from suite.mail.jmap.services.mail.mail import MailService
from suite.mail.jmap.services.mail.mailbox import MailboxService
from suite.mail.jmap.services.mail.submission.email_submission import EmailSubmissionService
from suite.mail.utils.dt import to_utc_z


class EmailService(MailService):
    """Service for handling email-related functionality based on the JMAP server capabilities."""

    type: ClassVar[str] = "Email"

    def create(self, emails: list[EmailCreateModel]) -> dict:
        """
        Public method to create email drafts and optionally submit them in one JMAP request.
        Returns the raw response envelope; method responses arrive in queue order (drafts first,
        then the submission, then any implicit responses), which Mail Queue reads positionally
        and persists as JSON.
        """

        batch = self._new_batch()
        handles, draft_refs = self._queue_drafts(batch, emails)

        submission_service = EmailSubmissionService(self.account, self.connection)
        handles.extend(submission_service._queue_submissions(batch, emails, draft_refs))

        self._run(batch)

        return self._to_envelope(handles)

    @staticmethod
    def _to_envelope(handles: list[Handle]) -> dict:
        """Reconstructs the raw `methodResponses` envelope from resolved handles, keeping each
        call's implicit responses (e.g. the Email/set emitted by onSuccessUpdateEmail) right
        after it, so positional consumers keep working."""

        method_responses = []
        for handle in handles:
            if handle.error is not None:
                method_responses.append(["error", handle.error.arguments, handle.call_id])
            else:
                method_responses.append([handle.call.name, handle.result, handle.call_id])

            for extra in handle.extra:
                method_responses.append([extra.name, extra.arguments, extra.method_call_id])

        return {"methodResponses": method_responses}

    def get(self, ids: list[str], properties: list[str] | None = None) -> list[dict]:
        """Public method to get emails, handling batching if a list of ids is provided and allowing optional specification of properties to retrieve."""

        properties = properties or [
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

        results = []
        if ids:
            for batch in self.create_batches(ids, self.max_objects_in_get):
                results.extend(self._get(batch, properties=properties, fetchAllBodyValues=True).get("list", []))
        else:
            results.extend(self._get(properties=properties, fetchAllBodyValues=True).get("list", []))

        return results

    def update(
        self, emails: list[dict], replace_keywords: bool = False, replace_mailboxes: bool = False
    ) -> dict:
        """Public method to update emails, handling batching if the number of emails exceeds the server's maximum allowed in a single 'set' call. Allows updating of keywords and mailboxIds."""

        result = {"updated": [], "notUpdated": {}}
        for batch in self.create_batches(emails, self.max_objects_in_set):
            payload = {}
            for email in batch:
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
                    raise ValueError(
                        "At least one of 'keywords' or 'mailbox_ids' must be provided for update."
                    )

            body = self._update(payload)

            result["updated"].extend(body.get("updated", {}).keys())
            if not_updated := body.get("notUpdated", {}):
                result["notUpdated"].update(not_updated)

        return result

    def delete(self, ids: list[str]) -> dict:
        """Public method to delete emails, handling batching if the number of ids exceeds the server's maximum allowed in a single 'set' call."""

        result = {"destroyed": [], "notDestroyed": {}}
        for batch in self.create_batches(ids, self.max_objects_in_set):
            body = self._delete(batch)

            result["destroyed"].extend(body.get("destroyed", []))
            if not_destroyed := body.get("notDestroyed", {}):
                result["notDestroyed"].update(not_destroyed)

        return result

    def query(
        self, filter: dict | None = None, position: int = 0, limit: int = 50, sort: list[dict] | None = None
    ) -> dict:
        """Public method to query emails, handling batching if the number of results exceeds the server's maximum allowed in a single 'query' call."""

        ids = []
        total = None
        batch_size = min(limit, self.max_objects_in_get)
        sort = sort or [{"property": "receivedAt", "isAscending": False}]

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
        """Public method to get changes to emails since a given state."""

        return self._changes(since_state)

    def search(self, text: str, limit: int = 50, separate_requests: bool = False) -> list[str]:
        """Public method to search for emails matching the given text in subject, to, cc, bcc, body or text."""

        filters = [
            {"subject": text},
            {"to": text},
            {"cc": text},
            {"bcc": text},
            {"body": text},
            {"text": text},
        ]

        return self._query_ids(filters, limit, separate_requests)

    def query_thread(
        self, filter: dict | None = None, position: int = 0, limit: int = 50, fetch_all: bool = False
    ) -> list[str] | dict[str, list[str]]:
        """Public method to query email threads, returning either a list of thread IDs or a mapping of thread IDs to email IDs of threads matching the filter."""

        batch = self._new_batch()
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

        self._run(batch)

        if not fetch_all:
            return self._read(query).get("ids", [])

        return {thread["id"]: thread.get("emailIds", []) for thread in self._read(threads).get("list", [])}

    def get_email_suggestions(self, text: str, limit: int = 5, separate_requests: bool = False) -> list[str]:
        """
        Get email suggestions based on the given text.

        Args:
                text (str): The text to search for in email addresses.
                limit (int): The maximum number of suggestions to return.
                separate_requests (bool): Whether to make separate requests for each filter.

        Returns:
                list[str]: A list of email addresses matching the given text.
        """

        addresses: list[str] = []

        filters = [
            {"from": text},
            {"to": text},
            {"cc": text},
            {"bcc": text},
        ]

        ids = self._query_ids(filters, limit, separate_requests)

        if not ids:
            return addresses

        emails = self.get(ids, properties=["from", "to", "cc", "bcc"])

        for email in emails:
            for field in ("from", "to", "cc", "bcc"):
                for addr in email.get(field) or []:
                    email_address = addr.get("email")
                    if (
                        email_address
                        and text.lower() in email_address.lower()
                        and email_address not in addresses
                    ):
                        addresses.append(email_address)

        return addresses[:limit]

    def _query_ids(self, filters: list[dict], limit: int, separate_requests: bool = False) -> list[str]:
        """Helper method to run one Email/query per filter (in one request, or one request each)
        and collect the unique email IDs across every result, capped at the limit."""

        ids: list[str] = []

        def collect_ids(handles: list[Handle]) -> None:
            for handle in handles:
                for id in self._read(handle).get("ids", []):
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
                batch = self._new_batch()
                handle = batch.add(f"{self._type}/query", query_args(filter))
                self._run(batch)
                collect_ids([handle])
        else:
            batch = self._new_batch()
            handles = [batch.add(f"{self._type}/query", query_args(filter)) for filter in filters]
            self._run(batch)
            collect_ids(handles)

        return ids[:limit]

    def _queue_drafts(self, batch: Batch, emails: list[EmailCreateModel]) -> tuple[list[Handle], dict]:
        """Helper method to queue draft creation for the given list of EmailCreateModel instances on the batch, returning the handles along with a mapping of creation IDs to draft references."""

        handles = []
        draft_refs = {}

        mailbox_service = MailboxService(self.account, self.connection)
        draft_mailbox_id = mailbox_service.get_mailbox_id_by_role(
            "drafts", create_if_not_exists=True, raise_exception=True
        )

        for email in emails:
            draft_ref = f"draft-{email.creation_id}"
            draft_refs[email.creation_id] = draft_ref

            # --------------------------------------------------
            # RAW MESSAGE → Email/import
            # --------------------------------------------------

            if email.raw_message:
                blob = self.upload_blob(email.raw_message.encode("utf-8"), content_type="message/rfc822")

                handles.append(
                    batch.add(
                        f"{self.type}/import",
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
                    handles.append(batch.add(f"{self.type}/set", {"destroy": [email.existing_id]}))

            # --------------------------------------------------
            # NORMAL DRAFT → Email/set
            # --------------------------------------------------

            else:
                payload = {"create": {draft_ref: self._get_draft(email, draft_mailbox_id)}}

                if email.existing_id:
                    payload["destroy"] = [email.existing_id]

                handles.append(batch.add(f"{self.type}/set", payload))

        return handles, draft_refs

    @staticmethod
    def _get_recipients(
        recipients: list[EmailRecipient], kind: Literal["to", "cc", "bcc"]
    ) -> list[dict[str, str | None]]:
        """Helper function to filter recipients by type and format them for the JMAP payload."""

        return [{"name": r.name, "email": r.email} for r in recipients if r.type == kind]

    @staticmethod
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
                if rcpts := EmailService._get_recipients(email.recipients, kind):
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
                "subParts": [body_root, *(EmailService._get_body_part(a) for a in inline_attachments)],
            }

            if regular_attachments:
                body_structure = {
                    "type": "multipart/mixed",
                    "subParts": [
                        body_structure,
                        *(EmailService._get_body_part(a) for a in regular_attachments),
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
                draft["attachments"] = [EmailService._get_body_part(a) for a in attachments]

        return draft

    @staticmethod
    def _get_body_part(attachment: EmailAttachment) -> dict[str, str]:
        """Helper function to build an EmailBodyPart payload for an attachment."""

        return {
            "name": attachment.name,
            "type": attachment.type,
            "cid": attachment.cid,
            "blobId": attachment.blob_id,
            "disposition": attachment.disposition,
        }
