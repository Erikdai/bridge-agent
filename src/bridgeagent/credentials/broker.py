"""Credential broker: secrets live in the OS keyring, never in a prompt.

Windows -> Credential Manager, macOS -> Keychain, Linux -> secret-service,
all through the keyring package. The planner only ever learns a site key
("uni-portal"); the executor resolves it at fill time.
"""

from __future__ import annotations

import keyring
import keyring.errors

SERVICE_PREFIX = "bridgeagent:"
_USERNAME_SLOT = "__username__"


class CredentialBroker:
    def set(self, site: str, username: str, password: str) -> None:
        keyring.set_password(SERVICE_PREFIX + site, _USERNAME_SLOT, username)
        keyring.set_password(SERVICE_PREFIX + site, username, password)

    def get(self, site: str) -> tuple[str, str] | None:
        username = keyring.get_password(SERVICE_PREFIX + site, _USERNAME_SLOT)
        if not username:
            return None
        password = keyring.get_password(SERVICE_PREFIX + site, username)
        if password is None:
            return None
        return username, password

    def delete(self, site: str) -> bool:
        username = keyring.get_password(SERVICE_PREFIX + site, _USERNAME_SLOT)
        if not username:
            return False
        try:
            keyring.delete_password(SERVICE_PREFIX + site, username)
            keyring.delete_password(SERVICE_PREFIX + site, _USERNAME_SLOT)
        except keyring.errors.PasswordDeleteError:
            return False
        return True

    def has(self, site: str) -> bool:
        return self.get(site) is not None
