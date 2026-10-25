"""
core/crypto.py — Application-level encryption at rest.

Encrypts secrets that would otherwise sit as plaintext on disk: LLM
provider API keys and the notification channels' *_token / *_password
settings (in settings.db; which keys is db/settings_db.is_secret_key) and saved login-session cookies
(data/cookies/*.json). This protects against someone reading the SQLite
file, a cookie JSON, or a backup of either directly — a stolen laptop,
a misconfigured file share, a leaked backup, a static-file route with a
path-traversal bug, etc.

Threat model, honestly stated: this does NOT protect against someone
with full control of the running server process itself, since the key
has to be readable by that process to do its job. Defending against
*that* needs the key to live somewhere the app server can't fully reach
on its own — a real KMS/secrets-manager/HSM, with the app requesting
decryption per-use rather than holding the key. That's the right answer
for a multi-tenant hosted deployment where other people's LinkedIn/
Indeed/Naukri sessions live on your infrastructure. This module is the
right-sized version for a self-hosted instance, and the foundation you'd
swap the key-loading function for if you move to a real KMS later —
everything above _load_or_create_key() stays the same either way.

Key: a Fernet key (symmetric, AES128-CBC + HMAC), auto-generated on
first run and stored in data/.master.key (chmod 600 on POSIX). BACK UP
THAT FILE — losing it permanently strands every encrypted value; there
is no recovery. For anything beyond a single self-hosted instance, set
APP_ENCRYPTION_KEY in the environment instead (a real secrets manager,
your platform's env-var vault, etc.) rather than relying on a key file
sitting next to the data it protects.
"""

import os
import stat
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.config import cfg

logger = logging.getLogger(__name__)

_KEY_PATH = cfg.DATA_DIR / ".master.key"
_ENC_PREFIX = "enc:"
_fernet = None


def _load_or_create_key() -> bytes:
    env_key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()

    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes().strip()

    key = Fernet.generate_key()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_bytes(key)
    try:
        os.chmod(_KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
    except Exception:
        pass  # best-effort; not all filesystems support POSIX perms
    logger.warning(
        f"[Crypto] Generated a new encryption key at {_KEY_PATH} — BACK IT UP. "
        "Losing this file makes every encrypted setting/cookie permanently "
        "unreadable. Set APP_ENCRYPTION_KEY in the environment instead if "
        "this deployment should manage the key externally."
    )
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string for storage. Empty strings pass through unchanged
    (no point encrypting "no API key set yet")."""
    if not plaintext:
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode()).decode()
    return _ENC_PREFIX + token


def decrypt(value: str) -> str:
    """
    Decrypt a stored string. Values without the enc: prefix are returned
    as-is — this is what makes the migration from an existing plaintext
    settings.db/cookies.json safe: old rows just aren't "encrypted" yet
    until the next time something writes them, rather than erroring out.
    """
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    try:
        return _get_fernet().decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("[Crypto] Could not decrypt a stored value — the "
                     "encryption key is missing/wrong, or the value is "
                     "corrupted. Returning empty rather than raising, so a "
                     "bad key doesn't take down the whole settings page.")
        return ""
    except Exception as e:
        logger.error(f"[Crypto] Unexpected decrypt error: {e}")
        return ""


def encrypt_bytes(data: bytes) -> bytes:
    """Same idea as encrypt(), for binary blobs (cookie JSON files)."""
    if not data:
        return data
    return _get_fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    """
    Decrypt a binary blob. Falls back to returning the input unchanged if
    it doesn't look like a Fernet token — this is the same "don't break
    on pre-existing plaintext" migration path as decrypt() above, since
    existing installs have plaintext cookie JSON files already on disk.
    """
    if not data:
        return data
    try:
        return _get_fernet().decrypt(data)
    except InvalidToken:
        return data  # looks like it was never encrypted — treat as plaintext
    except Exception as e:
        logger.error(f"[Crypto] Unexpected decrypt_bytes error: {e}")
        return data
