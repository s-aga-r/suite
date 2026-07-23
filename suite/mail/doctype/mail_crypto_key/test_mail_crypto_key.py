# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import datetime

import frappe
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12
from cryptography.x509.oid import NameOID
from frappe.tests import IntegrationTestCase, UnitTestCase

from suite.mail.utils.crypto import pgp, smime
from suite.mail.utils.crypto.detect import detect
from suite.mail.utils.crypto.mimeutil import build_content_mime, graft_headers, parse_message

HEADERS = [("From", "Alice <alice@example.com>"), ("To", "bob@example.com"), ("Subject", "Hi")]


def _self_signed(email: str = "alice@example.com"):
	key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
	name = x509.Name(
		[x509.NameAttribute(NameOID.COMMON_NAME, "Alice"), x509.NameAttribute(NameOID.EMAIL_ADDRESS, email)]
	)
	cert = (
		x509.CertificateBuilder()
		.subject_name(name)
		.issuer_name(name)
		.public_key(key.public_key())
		.serial_number(1)
		.not_valid_before(datetime.datetime(2026, 1, 1))
		.not_valid_after(datetime.datetime(2035, 1, 1))
		.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email)]), False)
		.sign(key, hashes.SHA256())
	)
	cert_pem = cert.public_bytes(Encoding.PEM)
	key_pem = key.private_bytes(
		Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
	)
	return cert, key, cert_pem, key_pem


class UnitTestMailCryptoKey(UnitTestCase):
	def test_smime_sign_verify_roundtrip(self):
		_, _, cert_pem, key_pem = _self_signed()
		content = build_content_mime("<p>hello</p>", "hello")
		full = graft_headers(smime.sign(content, cert_pem, key_pem), HEADERS)

		det = detect(parse_message(full))
		self.assertTrue(det.signed)
		self.assertEqual(det.protocol.value, "S/MIME")

		res = smime.verify(full)
		self.assertTrue(res.valid)
		self.assertEqual(res.signer, "alice@example.com")

	def test_smime_tamper_detected(self):
		_, _, cert_pem, key_pem = _self_signed()
		full = graft_headers(smime.sign(build_content_mime(None, "hello"), cert_pem, key_pem), HEADERS)
		tampered = full.replace(b"hello", b"HELLO")
		self.assertFalse(smime.verify(tampered).valid)

	def test_smime_encrypt_decrypt_roundtrip(self):
		_, _, cert_pem, key_pem = _self_signed()
		content = build_content_mime(None, "secret body")
		enc = smime.encrypt(content, [cert_pem])
		self.assertTrue(detect(parse_message(graft_headers(enc, HEADERS))).encrypted)
		self.assertIn(b"secret body", smime.decrypt(enc, cert_pem, key_pem))

	def test_pkcs12_extraction(self):
		cert, key, _, _ = _self_signed()
		p12 = pkcs12.serialize_key_and_certificates(
			b"alice", key, cert, None, serialization.BestAvailableEncryption(b"pw")
		)
		extracted = smime.extract_pkcs12(p12, "pw")
		self.assertEqual(extracted["email"], "alice@example.com")
		self.assertIn("BEGIN CERTIFICATE", extracted["certificate"])
		self.assertIn("BEGIN PRIVATE KEY", extracted["private_key"])

	def test_pgp_sign_verify_roundtrip(self):
		kp = pgp.generate("Alice <alice@example.com>")
		full = graft_headers(pgp.sign(build_content_mime("<p>hi</p>", "hi"), kp["private"]), HEADERS)
		self.assertTrue(detect(parse_message(full)).signed)
		res = pgp.verify(full, [kp["public"]])
		self.assertTrue(res.valid)
		self.assertEqual(res.signer, "alice@example.com")

	def test_pgp_tamper_detected(self):
		kp = pgp.generate("Alice <alice@example.com>")
		full = graft_headers(pgp.sign(build_content_mime(None, "hi"), kp["private"]), HEADERS)
		self.assertFalse(pgp.verify(full.replace(b"hi", b"HI"), [kp["public"]]).valid)

	def test_pgp_encrypt_decrypt_roundtrip(self):
		kp = pgp.generate("Alice <alice@example.com>")
		enc = pgp.encrypt(build_content_mime(None, "top secret"), [kp["public"]])
		self.assertTrue(detect(parse_message(graft_headers(enc, HEADERS))).encrypted)
		decrypted, _sig = pgp.decrypt(enc, kp["private"])
		self.assertIn(b"top secret", decrypted)


class IntegrationTestMailCryptoKey(IntegrationTestCase):
	def test_pgp_key_metadata_and_secret_roundtrip(self):
		kp = pgp.generate("Test <crypto-test@example.com>")
		doc = frappe.new_doc("Mail Crypto Key")
		doc.update(
			{
				"user": "Administrator",
				"email": "crypto-test@example.com",
				"protocol": "PGP",
				"certificate": kp["public"],
				"private_key": kp["private"],
			}
		)
		doc.insert(ignore_permissions=True)

		self.assertEqual(doc.fingerprint, kp["fingerprint"])
		reloaded = frappe.get_doc("Mail Crypto Key", doc.name)
		self.assertIn("BEGIN PGP PRIVATE KEY", reloaded.get_password("private_key"))

		doc.delete(ignore_permissions=True)

	def test_smime_certificate_metadata(self):
		_, _, cert_pem, key_pem = _self_signed("smime-test@example.com")
		doc = frappe.new_doc("Mail Crypto Key")
		doc.update(
			{
				"user": "Administrator",
				"email": "smime-test@example.com",
				"protocol": "S/MIME",
				"certificate": cert_pem.decode(),
				"private_key": key_pem.decode(),
			}
		)
		doc.insert(ignore_permissions=True)
		self.assertTrue(doc.fingerprint)
		self.assertEqual(doc.common_name, "Alice")
		doc.delete(ignore_permissions=True)
