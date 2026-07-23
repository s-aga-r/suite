# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime

from suite.mail.utils.crypto import Protocol
from suite.utils.permissions import OwnerFromUser


class MailCryptoKey(OwnerFromUser, Document):
	def validate(self) -> None:
		self.email = (self.email or "").strip().lower()
		self._populate_details()
		self._ensure_single_default()

	def _populate_details(self) -> None:
		"""Read fingerprint / common name / expiry out of the supplied material."""

		if self.protocol == Protocol.SMIME.value:
			from suite.mail.utils.crypto import smime

			try:
				identity = smime.cert_identity(smime.load_cert(self.certificate))
			except Exception as e:
				frappe.throw(_("Could not read the S/MIME certificate: {0}").format(e))

			if identity.email and self.email and identity.email != self.email:
				frappe.msgprint(
					_("Certificate address {0} does not match {1}.").format(identity.email, self.email),
					indicator="orange",
					alert=True,
				)
			self.fingerprint = identity.fingerprint
			self.common_name = identity.common_name
			self.not_valid_after = get_datetime(identity.not_valid_after) if identity.not_valid_after else None
			self.chain = self.chain or None
		else:
			from suite.mail.utils.crypto import pgp

			try:
				identity = pgp.cert_identity(pgp.load_public(self.certificate))
			except Exception as e:
				frappe.throw(_("Could not read the PGP key: {0}").format(e))

			self.fingerprint = identity.fingerprint
			self.common_name = identity.name
			self.chain = None

	def _ensure_single_default(self) -> None:
		if not self.is_default:
			return

		others = frappe.get_all(
			"Mail Crypto Key",
			filters={
				"user": self.user,
				"email": self.email,
				"protocol": self.protocol,
				"is_default": 1,
				"name": ("!=", self.name),
			},
			pluck="name",
		)
		for name in others:
			frappe.db.set_value("Mail Crypto Key", name, "is_default", 0)


def get_crypto_key(user: str, email: str, protocol: str | None = None) -> "MailCryptoKey | None":
	"""Return the default (or most recent) crypto key for an address.

	When ``protocol`` is given, only keys of that protocol are considered;
	otherwise S/MIME is preferred over PGP.
	"""

	filters = {"user": user, "email": (email or "").strip().lower()}
	if protocol:
		filters["protocol"] = protocol

	candidates = frappe.get_all(
		"Mail Crypto Key",
		filters=filters,
		fields=["name", "protocol", "is_default"],
		order_by="is_default desc, modified desc",
	)
	if not candidates:
		return None

	if not protocol:
		candidates.sort(key=lambda c: (0 if c.protocol == Protocol.SMIME.value else 1, 0 if c.is_default else 1))

	return frappe.get_doc("Mail Crypto Key", candidates[0].name)


def get_user_crypto_keys(user: str, protocol: str) -> list["MailCryptoKey"]:
	"""Return every crypto key a user holds for a protocol (used to try decryption)."""

	names = frappe.get_all(
		"Mail Crypto Key",
		filters={"user": user, "protocol": protocol},
		pluck="name",
		order_by="is_default desc, modified desc",
	)
	return [frappe.get_doc("Mail Crypto Key", name) for name in names]
