from suite.mail.jmap.services.core import CoreService


class ContactsService(CoreService):
    """Service for handling contact-related functionality based on the JMAP server capabilities."""

    @property
    def primary_account_id(self) -> str:
        """Returns the primary account ID for the logged-in user."""

        return self.connection.primary_accounts["urn:ietf:params:jmap:contacts"]
