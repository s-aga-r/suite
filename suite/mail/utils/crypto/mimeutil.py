"""Helpers to assemble the *content* MIME that gets signed/encrypted, and to
graft the outer envelope headers back on afterwards without disturbing the
protected bytes (which would break the signature)."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from email.message import Message

# S/MIME and PGP/MIME require CRLF line endings on the protected content.
SMIME_POLICY = policy.SMTP


def build_content_mime(
	html_body: str | None,
	text_body: str | None,
	attachments: list[dict] | None = None,
) -> bytes:
	"""Build the inner content part (body + attachments) as canonical MIME bytes.

	This is the payload that S/MIME signs/encrypts or PGP/MIME wraps. It carries
	no From/To/Subject — those are grafted on later by :func:`graft_headers`.

	Each attachment dict must provide ``content`` (bytes), ``filename``, ``type``
	(``maintype/subtype``), and may provide ``cid`` and ``disposition``.
	"""

	msg = EmailMessage(policy=SMIME_POLICY)

	if text_body and html_body:
		msg.set_content(text_body)
		msg.add_alternative(html_body, subtype="html")
	elif html_body:
		msg.set_content(html_body, subtype="html")
	else:
		msg.set_content(text_body or "")

	for attachment in attachments or []:
		content = attachment["content"]
		maintype, _, subtype = (attachment.get("type") or "application/octet-stream").partition("/")
		subtype = subtype or "octet-stream"
		cid = attachment.get("cid")
		disposition = attachment.get("disposition") or ("inline" if cid else "attachment")

		kwargs: dict = {"maintype": maintype, "subtype": subtype}
		if disposition == "inline" and cid:
			kwargs["cid"] = f"<{cid.strip('<>')}>"
			kwargs["disposition"] = "inline"
		else:
			kwargs["filename"] = attachment.get("filename")
			kwargs["disposition"] = "attachment"

		msg.add_attachment(content, **kwargs)

	return msg.as_bytes()


def _split_headers(raw: bytes) -> tuple[bytes, bytes, bytes]:
	"""Return (header_block, separator, body) splitting on the first blank line."""

	for sep in (b"\r\n\r\n", b"\n\n"):
		idx = raw.find(sep)
		if idx != -1:
			return raw[:idx], sep, raw[idx + len(sep) :]
	return raw, b"\r\n\r\n", b""


def graft_headers(protected: bytes, headers: list[tuple[str, str]]) -> bytes:
	"""Prepend envelope headers to an already signed/encrypted MIME message.

	The protected body bytes are left untouched so the signature stays valid.
	Any header already present in ``protected`` (e.g. ``Content-Type``,
	``MIME-Version``) is preserved; only the supplied envelope headers are added.
	"""

	head, sep, body = _split_headers(protected)
	line_ending = b"\r\n" if sep == b"\r\n\r\n" else b"\n"

	existing = {line.split(b":", 1)[0].strip().lower() for line in head.split(line_ending) if b":" in line}

	extra = b""
	for name, value in headers:
		if value is None:
			continue
		if name.lower() in existing:
			continue
		extra += name.encode() + b": " + str(value).encode() + line_ending

	return extra + head + sep + body


def parse_message(raw: bytes) -> "Message":
	"""Parse raw RFC822 bytes with the modern policy (descends into CMS/PGP-MIME)."""

	return BytesParser(policy=policy.default).parsebytes(raw)
