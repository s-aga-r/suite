"""Classify a parsed message as S/MIME- or PGP-protected so the receive path
knows which backend to invoke."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from suite.mail.utils.crypto import Protocol

if TYPE_CHECKING:
	from email.message import Message

_SMIME_SIGNATURE = {"application/pkcs7-signature", "application/x-pkcs7-signature"}
_SMIME_MIME = {"application/pkcs7-mime", "application/x-pkcs7-mime"}


@dataclass
class DetectResult:
	protocol: Protocol | None = None
	signed: bool = False
	encrypted: bool = False

	@property
	def is_protected(self) -> bool:
		return self.signed or self.encrypted


def detect(message: "Message") -> DetectResult:
	"""Inspect a parsed message's top-level content type for S/MIME or PGP-MIME."""

	ctype = message.get_content_type()
	protocol_param = (message.get_param("protocol") or "").lower()

	# --- S/MIME -----------------------------------------------------------
	if ctype == "multipart/signed" and protocol_param in _SMIME_SIGNATURE:
		return DetectResult(Protocol.SMIME, signed=True)

	if ctype in _SMIME_MIME:
		smime_type = (message.get_param("smime-type") or "").lower()
		if smime_type == "signed-data":
			return DetectResult(Protocol.SMIME, signed=True)
		# enveloped-data, or unspecified (treat as encrypted)
		return DetectResult(Protocol.SMIME, encrypted=True)

	# --- OpenPGP / PGP-MIME ----------------------------------------------
	if ctype == "multipart/signed" and protocol_param == "application/pgp-signature":
		return DetectResult(Protocol.PGP, signed=True)

	if ctype == "multipart/encrypted" and protocol_param == "application/pgp-encrypted":
		return DetectResult(Protocol.PGP, encrypted=True)

	# Inline PGP (rare): a text part beginning with an armor header.
	if ctype == "text/plain":
		payload = message.get_payload(decode=True) or b""
		head = payload[:64].lstrip()
		if head.startswith(b"-----BEGIN PGP MESSAGE-----"):
			return DetectResult(Protocol.PGP, encrypted=True)
		if head.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
			return DetectResult(Protocol.PGP, signed=True)

	return DetectResult()
