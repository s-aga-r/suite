"""Whitelisted endpoints for managing S/MIME and OpenPGP keys and for telling the
compose UI which addresses/recipients can be signed to or encrypted for."""

import base64

import frappe
from frappe import _

from suite.mail.doctype.user_account.user_account import get_user_for_jmap_account
from suite.mail.utils.crypto import Protocol


@frappe.whitelist()
def import_pkcs12(
	email: str,
	content: str,
	passphrase: str | None = None,
	label: str | None = None,
	is_default: bool = False,
	sign_by_default: bool = False,
	request_encryption: bool = False,
) -> dict:
	"""Create a :doctype:`Mail Crypto Key` from an uploaded, base64-encoded ``.p12``."""

	from suite.mail.utils.crypto import smime

	try:
		p12_bytes = base64.b64decode(content)
	except Exception:
		frappe.throw(_("Invalid PKCS#12 upload."))

	extracted = smime.extract_pkcs12(p12_bytes, passphrase)

	doc = frappe.new_doc("Mail Crypto Key")
	doc.update(
		{
			"user": frappe.session.user,
			"email": (email or extracted.get("email") or "").lower(),
			"protocol": Protocol.SMIME.value,
			"label": label,
			"certificate": extracted["certificate"],
			"chain": extracted["chain"] or None,
			"private_key": extracted["private_key"],
			"is_default": bool(is_default),
			"sign_by_default": bool(sign_by_default),
			"request_encryption": bool(request_encryption),
		}
	)
	doc.insert()
	return {"name": doc.name, "fingerprint": doc.fingerprint, "common_name": doc.common_name}


@frappe.whitelist()
def add_pem_key(
	email: str,
	protocol: str,
	certificate: str,
	private_key: str | None = None,
	chain: str | None = None,
	label: str | None = None,
	is_default: bool = False,
	sign_by_default: bool = False,
	request_encryption: bool = False,
) -> dict:
	"""Create a crypto key from pasted PEM (S/MIME) or ASCII-armored (PGP) material."""

	doc = frappe.new_doc("Mail Crypto Key")
	doc.update(
		{
			"user": frappe.session.user,
			"email": (email or "").lower(),
			"protocol": protocol,
			"label": label,
			"certificate": certificate,
			"chain": chain or None,
			"private_key": private_key or None,
			"is_default": bool(is_default),
			"sign_by_default": bool(sign_by_default),
			"request_encryption": bool(request_encryption),
		}
	)
	doc.insert()
	return {"name": doc.name, "fingerprint": doc.fingerprint, "common_name": doc.common_name}


@frappe.whitelist()
def generate_pgp_key(email: str, name: str | None = None, sign_by_default: bool = True) -> dict:
	"""Generate an OpenPGP keypair server-side and store it as a crypto key.

	Returns the public key so it can be shared/published. The private key stays
	encrypted in the record.
	"""

	from suite.mail.utils.crypto import pgp

	email = (email or "").lower()
	user_id = f"{name} <{email}>" if name else email
	kp = pgp.generate(user_id)

	doc = frappe.new_doc("Mail Crypto Key")
	doc.update(
		{
			"user": frappe.session.user,
			"email": email,
			"protocol": Protocol.PGP.value,
			"certificate": kp["public"],
			"private_key": kp["private"],
			"is_default": True,
			"sign_by_default": bool(sign_by_default),
		}
	)
	doc.insert()
	return {"name": doc.name, "fingerprint": doc.fingerprint, "public_key": kp["public"]}


@frappe.whitelist()
def add_peer_key(account: str, email: str, protocol: str, public_material: str) -> dict:
	"""Manually register a peer's public certificate/key for an account."""

	get_user_for_jmap_account(account, raise_exception=True)

	doc = frappe.new_doc("Mail Peer Key")
	doc.update(
		{
			"account": account,
			"email": (email or "").lower(),
			"protocol": protocol,
			"public_material": public_material,
			"source": "Manual",
		}
	)
	doc.insert()
	return {"name": doc.name, "fingerprint": doc.fingerprint}


@frappe.whitelist()
def get_signing_addresses() -> list[dict]:
	"""Addresses the current user can sign as, with the resolved protocol/defaults."""

	keys = frappe.get_all(
		"Mail Crypto Key",
		filters={"user": frappe.session.user},
		fields=["email", "protocol", "sign_by_default", "request_encryption", "is_default"],
	)
	by_email: dict[str, dict] = {}
	for k in keys:
		# Prefer the default (or first) key per address.
		if k.email not in by_email or k.is_default:
			by_email[k.email] = {
				"email": k.email,
				"protocol": k.protocol,
				"sign_by_default": bool(k.sign_by_default),
				"request_encryption": bool(k.request_encryption),
			}
	return list(by_email.values())


@frappe.whitelist()
def check_recipient_keys(account: str, emails: list[str] | str, protocol: str) -> dict:
	"""Report which recipient addresses have a peer key (encryption availability)."""

	get_user_for_jmap_account(account, raise_exception=True)

	if isinstance(emails, str):
		emails = frappe.parse_json(emails)

	have = set(
		frappe.get_all(
			"Mail Peer Key",
			filters={"account": account, "protocol": protocol, "email": ("in", [e.lower() for e in emails])},
			pluck="email",
		)
	)
	missing = [e for e in emails if e.lower() not in have]
	return {"can_encrypt": not missing, "missing": missing}
