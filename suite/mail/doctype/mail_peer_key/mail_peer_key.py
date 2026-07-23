# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime

from suite.mail.utils.crypto import Protocol


class MailPeerKey(Document):
	def validate(self) -> None:
		self.email = (self.email or "").strip().lower()
		self._populate_details()

	def _populate_details(self) -> None:
		if self.protocol == Protocol.SMIME.value:
			from suite.mail.utils.crypto import smime

			try:
				identity = smime.cert_identity(smime.load_cert(self.public_material))
			except Exception as e:
				frappe.throw(_("Could not read the S/MIME certificate: {0}").format(e))
			self.fingerprint = identity.fingerprint
			self.common_name = identity.common_name
			self.not_valid_after = get_datetime(identity.not_valid_after) if identity.not_valid_after else None
		else:
			from suite.mail.utils.crypto import pgp

			try:
				identity = pgp.cert_identity(pgp.load_public(self.public_material))
			except Exception as e:
				frappe.throw(_("Could not read the PGP key: {0}").format(e))
			self.fingerprint = identity.fingerprint
			self.common_name = identity.name


def get_peer_keys(account: str, email: str, protocol: str) -> list[str]:
	"""Return public-key material for a recipient/sender address (for a protocol)."""

	return frappe.get_all(
		"Mail Peer Key",
		filters={"account": account, "email": (email or "").strip().lower(), "protocol": protocol},
		pluck="public_material",
	)


def upsert_peer_key(
	account: str,
	email: str,
	protocol: str,
	public_material: str,
	fingerprint: str,
	source: str = "Harvested",
) -> None:
	"""Store a harvested/manual peer key, de-duplicated by fingerprint."""

	email = (email or "").strip().lower()
	if not (email and public_material and fingerprint):
		return

	exists = frappe.db.exists(
		"Mail Peer Key",
		{"account": account, "email": email, "protocol": protocol, "fingerprint": fingerprint},
	)
	if exists:
		return

	doc = frappe.new_doc("Mail Peer Key")
	doc.update(
		{
			"account": account,
			"email": email,
			"protocol": protocol,
			"public_material": public_material,
			"source": source,
		}
	)
	doc.insert(ignore_permissions=True)
