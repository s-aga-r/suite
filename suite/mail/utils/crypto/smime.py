"""S/MIME (CMS / PKCS#7) backend.

Sign, encrypt and decrypt use the native ``cryptography`` PKCS#7 API (no secret
material ever touches disk). Signature *verification* has no native API, so it
shells out to ``openssl cms -verify`` — canonicalisation-correct and operating
only on already-received, non-secret mail — while ``cryptography`` introspects
the signer certificate for identity/harvesting.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7, pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID

from suite.mail.utils.crypto import (
	DecryptionError,
	EncryptionError,
	SignResult,
	SigningError,
)
from suite.mail.utils.crypto.mimeutil import graft_headers

OPENSSL = shutil.which("openssl")


@dataclass
class CertIdentity:
	email: str | None
	common_name: str | None
	fingerprint: str
	not_valid_after: str | None = None


# ---------------------------------------------------------------------------
# Certificate / key material
# ---------------------------------------------------------------------------


def load_cert(cert_pem: bytes | str) -> x509.Certificate:
	data = cert_pem.encode() if isinstance(cert_pem, str) else cert_pem
	if b"-----BEGIN" in data:
		return x509.load_pem_x509_certificate(data)
	return x509.load_der_x509_certificate(data)


def load_private_key(key_pem: bytes | str, password: bytes | None = None):
	data = key_pem.encode() if isinstance(key_pem, str) else key_pem
	return serialization.load_pem_private_key(data, password=password)


def cert_identity(cert: x509.Certificate) -> CertIdentity:
	"""Extract the RFC822 email, CN and SHA-256 fingerprint from a certificate."""

	email = None
	try:
		san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
		emails = san.value.get_values_for_type(x509.RFC822Name)
		email = emails[0] if emails else None
	except x509.ExtensionNotFound:
		pass

	if not email:
		attrs = cert.subject.get_attributes_for_oid(NameOID.EMAIL_ADDRESS)
		email = attrs[0].value if attrs else None

	cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
	common_name = cn_attrs[0].value if cn_attrs else None

	fingerprint = cert.fingerprint(hashes.SHA256()).hex()
	try:
		not_after_dt = cert.not_valid_after_utc
	except AttributeError:  # cryptography < 42
		not_after_dt = cert.not_valid_after
	# Store as naive UTC — MariaDB's DATETIME rejects a tz offset.
	not_after = not_after_dt.replace(tzinfo=None).isoformat(sep=" ")

	return CertIdentity(email=email and email.lower(), common_name=common_name, fingerprint=fingerprint, not_valid_after=not_after)


def extract_pkcs12(p12_bytes: bytes, passphrase: str | None) -> dict:
	"""Unpack an uploaded ``.p12``/``.pfx`` into PEM key + cert (+ chain)."""

	password = passphrase.encode() if passphrase else None
	try:
		key, cert, chain = pkcs12.load_key_and_certificates(p12_bytes, password)
	except Exception as e:
		raise SigningError(f"Could not open PKCS#12 bundle: {e}") from e

	if not key or not cert:
		raise SigningError("PKCS#12 bundle does not contain a private key and certificate.")

	identity = cert_identity(cert)
	return {
		"private_key": key.private_bytes(
			Encoding.PEM,
			serialization.PrivateFormat.PKCS8,
			serialization.NoEncryption(),
		).decode(),
		"certificate": cert.public_bytes(Encoding.PEM).decode(),
		"chain": b"".join(c.public_bytes(Encoding.PEM) for c in (chain or [])).decode(),
		"email": identity.email,
		"common_name": identity.common_name,
		"fingerprint": identity.fingerprint,
		"not_valid_after": identity.not_valid_after,
	}


# ---------------------------------------------------------------------------
# Sign / encrypt / decrypt (native)
# ---------------------------------------------------------------------------


def sign(content_mime: bytes, cert_pem: bytes | str, key_pem: bytes | str, chain_pem: bytes | str = b"") -> bytes:
	"""Return an ``multipart/signed`` S/MIME message wrapping ``content_mime``."""

	cert = load_cert(cert_pem)
	key = load_private_key(key_pem)
	builder = pkcs7.PKCS7SignatureBuilder().set_data(content_mime).add_signer(cert, key, hashes.SHA256())

	chain = chain_pem.encode() if isinstance(chain_pem, str) else chain_pem
	if chain and b"-----BEGIN" in chain:
		for extra in x509.load_pem_x509_certificates(chain):
			builder = builder.add_certificate(extra)

	try:
		return builder.sign(Encoding.SMIME, [pkcs7.PKCS7Options.DetachedSignature])
	except Exception as e:
		raise SigningError(f"S/MIME signing failed: {e}") from e


def encrypt(content_mime: bytes, recipient_certs_pem: list[bytes | str]) -> bytes:
	"""Return an ``application/pkcs7-mime`` enveloped message for the recipients."""

	if not recipient_certs_pem:
		raise EncryptionError("No recipient certificates available for S/MIME encryption.")

	builder = pkcs7.PKCS7EnvelopeBuilder().set_data(content_mime)
	for cert_pem in recipient_certs_pem:
		builder = builder.add_recipient(load_cert(cert_pem))

	try:
		return builder.encrypt(Encoding.SMIME, [])
	except Exception as e:
		raise EncryptionError(f"S/MIME encryption failed: {e}") from e


def sign_and_encrypt(
	content_mime: bytes,
	cert_pem: bytes | str,
	key_pem: bytes | str,
	recipient_certs_pem: list[bytes | str],
	chain_pem: bytes | str = b"",
) -> bytes:
	"""Sign then encrypt (triple-wrapping): recipients see a verified inner signature."""

	# The enveloped content is the complete signed MIME entity (headers + body).
	signed = sign(content_mime, cert_pem, key_pem, chain_pem)
	return encrypt(signed, recipient_certs_pem)


def decrypt(enveloped_message: bytes, cert_pem: bytes | str, key_pem: bytes | str) -> bytes:
	"""Decrypt an S/MIME enveloped message, returning the inner MIME bytes."""

	cert = load_cert(cert_pem)
	key = load_private_key(key_pem)
	try:
		return pkcs7.pkcs7_decrypt_smime(enveloped_message, cert, key, [])
	except Exception as e:
		raise DecryptionError(f"S/MIME decryption failed: {e}") from e


# ---------------------------------------------------------------------------
# Verify (openssl CLI for the signature, cryptography for signer identity)
# ---------------------------------------------------------------------------


def _signer_certs_from_signed(signed_message: bytes) -> list[x509.Certificate]:
	"""Pull the embedded signer certificate(s) out of a multipart/signed message."""

	from email.parser import BytesParser
	from email import policy

	msg = BytesParser(policy=policy.default).parsebytes(signed_message)
	for part in msg.walk():
		ctype = part.get_content_type()
		if ctype in ("application/pkcs7-signature", "application/x-pkcs7-signature"):
			der = part.get_payload(decode=True)
			try:
				return pkcs7.load_der_pkcs7_certificates(der)
			except Exception:
				return []
	return []


def verify(signed_message: bytes, trusted_roots_pem: bytes | str | None = None) -> SignResult:
	"""Verify a ``multipart/signed`` S/MIME message.

	Signature validity is checked with ``openssl cms -verify -noverify`` (no chain
	trust); if ``trusted_roots_pem`` is supplied, a second pass sets ``trusted``.
	The signer identity is read from the embedded certificate.
	"""

	result = SignResult(valid=False)

	certs = _signer_certs_from_signed(signed_message)
	if certs:
		# The signer cert is the leaf (the one that is not a CA / issues nothing else here).
		signer = certs[0]
		identity = cert_identity(signer)
		result.signer = identity.email
		result.signer_name = identity.common_name
		result.signer_fingerprint = identity.fingerprint
		result.signer_public_material = signer.public_bytes(Encoding.PEM).decode()

	if not OPENSSL:
		result.errors.append("openssl unavailable: S/MIME signature not verified")
		return result

	with tempfile.TemporaryDirectory() as tmp:
		tmpdir = Path(tmp)
		msg_path = tmpdir / "message.eml"
		msg_path.write_bytes(signed_message)

		proc = subprocess.run(
			[OPENSSL, "cms", "-verify", "-noverify", "-in", str(msg_path), "-inform", "SMIME", "-out", "/dev/null"],
			capture_output=True,
		)
		result.valid = proc.returncode == 0
		if not result.valid:
			result.errors.append((proc.stderr or b"").decode("utf-8", "ignore").strip() or "signature verification failed")
			return result

		if trusted_roots_pem:
			roots = trusted_roots_pem.encode() if isinstance(trusted_roots_pem, str) else trusted_roots_pem
			ca_path = tmpdir / "roots.pem"
			ca_path.write_bytes(roots)
			trust_proc = subprocess.run(
				[OPENSSL, "cms", "-verify", "-in", str(msg_path), "-inform", "SMIME", "-CAfile", str(ca_path), "-out", "/dev/null"],
				capture_output=True,
			)
			result.trusted = trust_proc.returncode == 0

	return result
