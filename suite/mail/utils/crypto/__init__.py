"""End-to-end message crypto (S/MIME and OpenPGP) for Suite Mail.

Stalwart/JMAP only offer encryption-at-rest, so the four end-user operations —
sign & encrypt on send, verify & decrypt on receive — are performed here, in the
Frappe backend, keeping the Vue client thin. Outbound protected messages are
submitted as raw RFC822 through the existing ``Email/import`` path; inbound ones
are processed from the raw blob in ``format_message``.

The two protocol backends (:mod:`.smime`, :mod:`.pgp`) expose the same four
verbs and share the dataclasses defined here. :mod:`.detect` classifies a parsed
message so the receive path knows which backend (if any) to invoke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Protocol(str, Enum):
	SMIME = "S/MIME"
	PGP = "PGP"


class CryptoError(Exception):
	"""Base class for all message-crypto failures."""


class NoKeyError(CryptoError):
	"""Raised when the key material required for an operation is missing."""


class DecryptionError(CryptoError):
	"""Raised when a message cannot be decrypted with the available key."""


class SigningError(CryptoError):
	"""Raised when a message cannot be signed."""


class EncryptionError(CryptoError):
	"""Raised when a message cannot be encrypted for the given recipients."""


@dataclass
class MessageSecurity:
	"""Verdict attached to an incoming message and surfaced to the client.

	``signature_valid`` is ``None`` when the message carries no signature or the
	signature could not be checked (see ``errors``); ``trusted`` additionally
	requires the signer certificate to chain to a configured/root CA.
	"""

	protocol: Protocol | None = None
	signed: bool = False
	encrypted: bool = False
	signature_valid: bool | None = None
	trusted: bool = False
	signer: str | None = None
	signer_name: str | None = None
	signer_fingerprint: str | None = None
	errors: list[str] = field(default_factory=list)

	def to_dict(self) -> dict:
		return {
			"protocol": self.protocol.value if self.protocol else None,
			"signed": self.signed,
			"encrypted": self.encrypted,
			"signature_valid": self.signature_valid,
			"trusted": self.trusted,
			"signer": self.signer,
			"signer_name": self.signer_name,
			"signer_fingerprint": self.signer_fingerprint,
			"errors": self.errors,
		}


@dataclass
class SignResult:
	"""Outcome of verifying a signature: validity plus harvested signer identity."""

	valid: bool
	trusted: bool = False
	signer: str | None = None
	signer_name: str | None = None
	signer_fingerprint: str | None = None
	signer_public_material: str | None = None
	errors: list[str] = field(default_factory=list)
