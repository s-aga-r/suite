"""OpenPGP / PGP-MIME (RFC 3156) backend, built on pysequoia.

pysequoia is a maintained, Rust-backed OpenPGP implementation shipped as a
binary wheel (works on Python 3.14, needs no ``gpg`` binary). The multipart
structures are assembled and split by hand so the signed content bytes are
preserved verbatim across the sign→verify round-trip.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import pysequoia
from pysequoia import Cert, SignatureMode, Sig

from suite.mail.utils.crypto import (
	DecryptionError,
	EncryptionError,
	NoKeyError,
	SignResult,
	SigningError,
)

CRLF = b"\r\n"


@dataclass
class PgpIdentity:
	email: str | None
	name: str | None
	fingerprint: str


def _boundary() -> str:
	return f"=-=_{secrets.token_hex(16)}"


def load_public(material: str | bytes) -> Cert:
	data = material if isinstance(material, bytes) else material.encode()
	return Cert.from_bytes(data)


def cert_identity(cert: Cert) -> PgpIdentity:
	uids = cert.user_ids
	name = email = None
	if uids:
		value = str(uids[0].value if hasattr(uids[0], "value") else uids[0])
		# UserID is typically "Name <email>"
		if "<" in value and ">" in value:
			name = value.split("<", 1)[0].strip() or None
			email = value.split("<", 1)[1].split(">", 1)[0].strip().lower()
		elif "@" in value:
			email = value.strip().lower()
		else:
			name = value.strip() or None
	return PgpIdentity(email=email, name=name, fingerprint=str(cert.fingerprint))


def generate(user_id: str) -> dict:
	"""Generate a fresh keypair (used by tests and optional in-app enrolment)."""

	cert = Cert.generate(user_id)
	identity = cert_identity(cert)
	return {
		"public": str(cert),
		"private": str(cert.secrets),
		"fingerprint": identity.fingerprint,
		"email": identity.email,
		"name": identity.name,
	}


# ---------------------------------------------------------------------------
# Sign / encrypt
# ---------------------------------------------------------------------------


def sign(content_mime: bytes, private_material: str | bytes) -> bytes:
	"""Return a PGP/MIME ``multipart/signed`` message wrapping ``content_mime``."""

	try:
		cert = load_public(private_material)
		if not cert.has_secret_keys:
			raise NoKeyError("PGP signing key has no secret material.")
		signer = cert.secrets.signer()
		armored_sig = pysequoia.sign(signer, content_mime, mode=SignatureMode.DETACHED)
	except NoKeyError:
		raise
	except Exception as e:
		raise SigningError(f"PGP signing failed: {e}") from e

	sig_bytes = armored_sig.encode() if isinstance(armored_sig, str) else armored_sig
	boundary = _boundary()
	b = boundary.encode()

	headers = (
		b'Content-Type: multipart/signed; micalg="pgp-sha256"; '
		b'protocol="application/pgp-signature";' + CRLF + b' boundary="' + b + b'"' + CRLF + b"MIME-Version: 1.0" + CRLF
	)
	sig_part = (
		b'Content-Type: application/pgp-signature; name="signature.asc"' + CRLF
		+ b"Content-Description: OpenPGP digital signature" + CRLF
		+ b'Content-Disposition: attachment; filename="signature.asc"' + CRLF + CRLF
		+ sig_bytes
	)
	body = (
		b"--" + b + CRLF
		+ content_mime + CRLF
		+ b"--" + b + CRLF
		+ sig_part + CRLF
		+ b"--" + b + b"--" + CRLF
	)
	return headers + CRLF + body


def encrypt(content_mime: bytes, recipient_materials: list[str | bytes], sign_with: str | bytes | None = None) -> bytes:
	"""Return a PGP/MIME ``multipart/encrypted`` message for the recipients."""

	if not recipient_materials:
		raise EncryptionError("No recipient public keys available for PGP encryption.")

	try:
		recipients = [load_public(m) for m in recipient_materials]
		kwargs: dict = {"recipients": recipients}
		if sign_with:
			signer_cert = load_public(sign_with)
			if signer_cert.has_secret_keys:
				kwargs["signer"] = signer_cert.secrets.signer()
		armored = pysequoia.encrypt(content_mime, **kwargs)
	except Exception as e:
		raise EncryptionError(f"PGP encryption failed: {e}") from e

	enc_bytes = armored.encode() if isinstance(armored, str) else armored
	boundary = _boundary()
	b = boundary.encode()

	headers = (
		b'Content-Type: multipart/encrypted; protocol="application/pgp-encrypted";' + CRLF
		+ b' boundary="' + b + b'"' + CRLF + b"MIME-Version: 1.0" + CRLF
	)
	body = (
		b"--" + b + CRLF
		+ b"Content-Type: application/pgp-encrypted" + CRLF
		+ b"Content-Description: PGP/MIME version identification" + CRLF + CRLF
		+ b"Version: 1" + CRLF + CRLF
		+ b"--" + b + CRLF
		+ b'Content-Type: application/octet-stream; name="encrypted.asc"' + CRLF
		+ b"Content-Description: OpenPGP encrypted message" + CRLF
		+ b'Content-Disposition: inline; filename="encrypted.asc"' + CRLF + CRLF
		+ enc_bytes + CRLF
		+ b"--" + b + b"--" + CRLF
	)
	return headers + CRLF + body


# ---------------------------------------------------------------------------
# Verify / decrypt
# ---------------------------------------------------------------------------


def _split_signed(signed_message: bytes) -> tuple[bytes, bytes]:
	"""Return (signed_content_bytes, detached_signature_bytes) from multipart/signed.

	The content is extracted verbatim between boundary delimiters so the bytes
	hashed for verification exactly match what was signed.
	"""

	from email.parser import BytesParser
	from email import policy

	msg = BytesParser(policy=policy.default).parsebytes(signed_message)
	boundary = msg.get_boundary()
	if not boundary:
		raise SigningError("multipart/signed message has no boundary.")

	_, _, rest = signed_message.partition(b"\r\n\r\n") if b"\r\n\r\n" in signed_message else signed_message.partition(b"\n\n")
	delimiter = b"--" + boundary.encode()
	segments = rest.split(delimiter)
	# segments[0] = preamble, [1] = content, [2] = signature part, [3] = epilogue after closing --
	if len(segments) < 3:
		raise SigningError("multipart/signed message is malformed.")

	content = segments[1]
	# Strip the single CRLF that follows the opening delimiter and the one that precedes the next.
	if content.startswith(b"\r\n"):
		content = content[2:]
	elif content.startswith(b"\n"):
		content = content[1:]
	if content.endswith(b"\r\n"):
		content = content[:-2]
	elif content.endswith(b"\n"):
		content = content[:-1]

	sig_segment = segments[2]
	sig_start = sig_segment.find(b"-----BEGIN PGP SIGNATURE-----")
	sig_end = sig_segment.find(b"-----END PGP SIGNATURE-----")
	if sig_start == -1 or sig_end == -1:
		raise SigningError("PGP signature block not found.")
	signature = sig_segment[sig_start : sig_end + len(b"-----END PGP SIGNATURE-----")]

	return content, signature


def verify(signed_message: bytes, known_public_materials: list[str | bytes] | None = None) -> SignResult:
	"""Verify a PGP/MIME ``multipart/signed`` message against known public keys."""

	result = SignResult(valid=False)
	try:
		content, signature = _split_signed(signed_message)
		sig = Sig.from_bytes(signature)
	except Exception as e:
		result.errors.append(str(e))
		return result

	certs = []
	for material in known_public_materials or []:
		try:
			certs.append(load_public(material))
		except Exception:
			continue

	def store(_key_ids):
		return certs

	try:
		verified = pysequoia.verify(bytes=content, signature=sig, store=store)
		result.valid = True
		result.trusted = bool(certs)
	except Exception as e:
		result.errors.append(f"PGP signature verification failed: {e}")
		return result

	# Resolve the signer certificate from the verified signature's fingerprint.
	signer_fpr = None
	for valid_sig in getattr(verified, "valid_sigs", None) or []:
		signer_fpr = str(getattr(valid_sig, "certificate", "")).lower()
		break

	signer_cert = None
	if signer_fpr:
		signer_cert = next((c for c in certs if str(c.fingerprint).lower() == signer_fpr), None)
	if signer_cert is None and len(certs) == 1:
		signer_cert = certs[0]

	if signer_cert is not None:
		identity = cert_identity(signer_cert)
		result.signer = identity.email
		result.signer_name = identity.name
		result.signer_fingerprint = identity.fingerprint
		result.signer_public_material = str(signer_cert)
	return result


def decrypt(encrypted_message: bytes, private_material: str | bytes) -> tuple[bytes, SignResult | None]:
	"""Decrypt a PGP/MIME ``multipart/encrypted`` message.

	Returns the inner MIME bytes and, if the encrypted payload was also signed,
	a :class:`SignResult` describing the embedded signature.
	"""

	from email.parser import BytesParser
	from email import policy

	msg = BytesParser(policy=policy.default).parsebytes(encrypted_message)
	armored = None
	for part in msg.walk():
		if part.get_content_type() == "application/octet-stream":
			armored = part.get_payload(decode=True)
			break
	if armored is None:
		raise DecryptionError("PGP/MIME encrypted part not found.")

	try:
		cert = load_public(private_material)
		if not cert.has_secret_keys:
			raise NoKeyError("PGP decryption key has no secret material.")
		decrypted = pysequoia.decrypt(armored, decryptor=cert.secrets.decryptor())
	except NoKeyError:
		raise
	except Exception as e:
		raise DecryptionError(f"PGP decryption failed: {e}") from e

	sig_result = None
	if getattr(decrypted, "valid_sigs", None):
		sig_result = SignResult(valid=True, trusted=True)
	return decrypted.bytes, sig_result
