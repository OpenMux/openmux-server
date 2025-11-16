"""Authentication management utilities for the OpenMux server.

Provides a lightweight credential + API key verification layer with:
* Password hashing / comparison (SHA-256, constant-time compare)
* Short‑term authentication result caching to reduce hashing cost
* API key permission lookups
* Helpers to hash passwords and generate random API keys
"""

import base64
import hashlib
import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AuthManager:
    def __init__(self, config: Dict[str, Any]):
        """Initialize the authentication manager.

        Args:
            config: Configuration mapping containing optional "users" and
                "api_keys" sequences. User entries require "username" and
                "password_hash" fields (SHA-256 hex). API key entries require
                "key" and may include "permissions".
        """
        self.config = config
        self.logger = logging.getLogger("openmux.auth")

        # Cache for authentication results to avoid excessive hashing
        self.auth_cache = {}

        # Extract users and API keys from config for easier access
        self.users = config.get("users", [])  # noqa: Vulture (accessed dynamically)
        self.api_keys = config.get("api_keys", [])  # noqa: Vulture (accessed dynamically)
        # Public key records: list of dicts with fields: username?, key_id, public_key, allowed_uses?
        # allowed_uses is a list of contexts this key may be used for, e.g., ["client"], ["muxcon"], ["client","muxcon"]
        self.public_keys = self._normalize_public_keys(config.get("public_keys", []))
        # PAM configuration (optional)
        pam_cfg = config.get("pam", {}) or {}
        self._pam_enabled = bool(pam_cfg.get("enabled", False))
        self._pam_service = str(pam_cfg.get("service_name", "login"))
        # Policy controls
        # allow_root: default False (root logins disabled unless explicitly allowed)
        self._pam_allow_root = bool(pam_cfg.get("allow_root", False))
        allowed_users_cfg = pam_cfg.get("allowed_users")
        self._pam_allowed_users = set(allowed_users_cfg) if isinstance(allowed_users_cfg, list) else None
        # Group to role mapping (configurable group names)
        default_groups = {
            "admin_group": "openmux_admin",
            "write_group": "openmux_write",
            "read_group": "openmux_read",
        }
        groups_cfg = pam_cfg.get("groups") or {}
        self._pam_groups = {
            "admin_group": str(groups_cfg.get("admin_group", default_groups["admin_group"])),
            "write_group": str(groups_cfg.get("write_group", default_groups["write_group"])),
            "read_group": str(groups_cfg.get("read_group", default_groups["read_group"])),
        }
        # Active pubkey challenges: (username, key_id) -> {nonce_raw, pubkey, expires_at}
        self._pk_challenges = {}
        # Active password HMAC challenges: username -> {nonce_raw, expires_at, pw_hex}
        self._pw_hmac_challenges = {}
        # Authentication failure tracking: (username, ip) -> {failures, first_failure, locked_until}
        self._fail_tracker = {}
        # Policy parameters (could be made configurable)
        self._max_fail_window = 300  # seconds window for counting failures
        self._base_lock_seconds = 30  # initial lock duration after threshold
        self._failure_threshold = 5  # failures before lock engages

    async def update_config(self, new_config: Dict[str, Any]):
        """Replace the active authentication configuration.

        Clears the in-memory auth cache so subsequent calls re-evaluate
        credentials against the new configuration.

        Args:
            new_config: New configuration dictionary with same shape as the
                original initialization config.
        """
        self.config = new_config
        # Update internal references
        self.users = new_config.get("users", [])  # noqa: Vulture (dynamic update)
        self.api_keys = new_config.get("api_keys", [])  # noqa: Vulture (dynamic update)
        self.public_keys = self._normalize_public_keys(new_config.get("public_keys", []))
        # Update PAM config
        pam_cfg = new_config.get("pam", {}) or {}
        self._pam_enabled = bool(pam_cfg.get("enabled", False))
        self._pam_service = str(pam_cfg.get("service_name", "login"))
        # allow_root: default False (root logins disabled unless explicitly allowed)
        self._pam_allow_root = bool(pam_cfg.get("allow_root", False))
        allowed_users_cfg = pam_cfg.get("allowed_users")
        self._pam_allowed_users = set(allowed_users_cfg) if isinstance(allowed_users_cfg, list) else None
        groups_cfg = pam_cfg.get("groups") or {}
        self._pam_groups = {
            "admin_group": str(groups_cfg.get("admin_group", "openmux_admin")),
            "write_group": str(groups_cfg.get("write_group", "openmux_write")),
            "read_group": str(groups_cfg.get("read_group", "openmux_read")),
        }
        # Clear cache when config changes
        self.auth_cache = {}
        # Clear outstanding challenges (they reference old keys)
        self._pk_challenges.clear()
        self._pw_hmac_challenges.clear()

    # =================== Password HMAC Challenge (Upgrade Path) ===================
    def _normalize_public_keys(self, pk_list):
        """Normalize public key records:

        - Ensure each record is a dict with key_id and public_key
        - Normalize allowed_uses to a lowercase list
        - Default allowed_uses:
            * If username present and allowed_uses missing -> ["client"]
            * If username missing and allowed_uses missing -> ["muxcon"] (backward-compat friendly)
        - Preserve unknown fields for round-trip friendliness
        """
        norm = []
        try:
            for rec in pk_list or []:
                if not isinstance(rec, dict):
                    continue
                r = dict(rec)
                # Normalize allowed_uses
                au = r.get("allowed_uses")
                if au is None:
                    # Default based on presence of username
                    if r.get("username"):
                        r["allowed_uses"] = ["client"]
                    else:
                        r["allowed_uses"] = ["muxcon"]
                else:
                    if isinstance(au, str):
                        r["allowed_uses"] = [au.lower()]
                    elif isinstance(au, list):
                        r["allowed_uses"] = [str(x).lower() for x in au]
                    else:
                        r["allowed_uses"] = ["client"] if r.get("username") else ["muxcon"]
                norm.append(r)
        except Exception:
            # On normalization error, fall back to raw list to avoid breaking startup
            return pk_list or []
        return norm

    def get_password_hash_hex(self, username: str) -> Optional[str]:
        for u in self.users:
            if u.get("username") == username:
                return u.get("password_hash")
        return None

    def start_password_hmac_challenge(self, username: str) -> Optional[str]:
        """Initiate HMAC password challenge for a user.

        Returns base64 nonce or None if user not found.
        """
        pw_hex = self.get_password_hash_hex(username)
        if not pw_hex:
            return None
        import base64
        import secrets
        import time

        nonce_raw = secrets.token_bytes(32)
        nonce_b64 = base64.b64encode(nonce_raw).decode()
        self._pw_hmac_challenges[username] = {
            "nonce_raw": nonce_raw,
            "expires_at": time.time() + 30,
            "pw_hex": pw_hex,
        }
        return nonce_b64

    def verify_password_hmac(self, username: str, hmac_b64: str, src_ip: Optional[str] = None) -> bool:
        # Pre-check lock status
        if self.is_user_locked(username, src_ip):
            return False
        entry = self._pw_hmac_challenges.get(username)
        if not entry:
            self.register_auth_failure(username, src_ip)
            return False
        import base64
        import hashlib
        import hmac
        import time

        if time.time() > entry.get("expires_at", 0):
            self._pw_hmac_challenges.pop(username, None)
            self.register_auth_failure(username, src_ip)
            return False
        try:
            received = base64.b64decode(hmac_b64)
        except Exception:
            self._pw_hmac_challenges.pop(username, None)
            self.register_auth_failure(username, src_ip)
            return False
        pw_hex = entry.get("pw_hex")
        if not pw_hex:
            self._pw_hmac_challenges.pop(username, None)
            self.register_auth_failure(username, src_ip)
            return False
        try:
            key_bytes = bytes.fromhex(pw_hex)
        except Exception:
            self._pw_hmac_challenges.pop(username, None)
            self.register_auth_failure(username, src_ip)
            return False
        expected = hmac.new(key_bytes, entry["nonce_raw"], hashlib.sha256).digest()
        ok = hmac.compare_digest(expected, received)
        # Single-use
        self._pw_hmac_challenges.pop(username, None)
        if not ok:
            self.register_auth_failure(username, src_ip)
        else:
            self.clear_auth_failures(username, src_ip)
        return ok

    # =================== Failure Tracking / Lockout ===================
    def register_auth_failure(self, username: str, src_ip: Optional[str]):
        now = time.time()
        key = (username, src_ip or "?")
        rec = self._fail_tracker.get(key)
        if not rec:
            self._fail_tracker[key] = {"failures": 1, "first_failure": now, "locked_until": 0}
            return
        # Reset window if expired
        if now - rec.get("first_failure", now) > self._max_fail_window:
            rec["failures"] = 1
            rec["first_failure"] = now
            rec["locked_until"] = 0
            return
        rec["failures"] += 1
        if rec["failures"] >= self._failure_threshold:
            # Exponential lock duration: base * 2^(failures - threshold)
            exponent = rec["failures"] - self._failure_threshold
            lock_seconds = self._base_lock_seconds * (2**exponent)
            # Cap lock at 1 hour
            lock_seconds = min(lock_seconds, 3600)
            rec["locked_until"] = max(rec.get("locked_until", 0), now + lock_seconds)

    def clear_auth_failures(self, username: str, src_ip: Optional[str]):
        key = (username, src_ip or "?")
        if key in self._fail_tracker:
            self._fail_tracker.pop(key, None)

    def is_user_locked(self, username: str, src_ip: Optional[str]) -> bool:
        key = (username, src_ip or "?")
        rec = self._fail_tracker.get(key)
        if not rec:
            return False
        locked_until = rec.get("locked_until", 0)
        if locked_until <= time.time():
            # Auto clear after lock expiry (per IP)
            if rec.get("failures") < self._failure_threshold:
                self._fail_tracker.pop(key, None)
            else:
                rec["locked_until"] = 0
            return False
        return True

    # =================== Public Key (Ed25519) Authentication ===================
    def _resolve_public_key_record(self, username: str, key_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the matching public key record for a user.

        If key_id is None and multiple keys exist, returns the first active one.
        """
        # Only consider keys permitted for client use
        matches = [
            r
            for r in self.public_keys
            if r.get("username") == username and not r.get("disabled") and ("client" in (r.get("allowed_uses") or []))
        ]
        if not matches:
            return None
        if key_id:
            for r in matches:
                if r.get("key_id") == key_id:
                    return r
            return None
        return matches[0]

    def get_public_keys_for_use(self, use: str) -> list:
        """Return list of public key records allowed for a specific use.

        Args:
            use: Usage context, e.g. "client" or "muxcon".

        Returns:
            List of public key records with fields including key_id and public_key.
        """
        use_l = str(use).lower()
        try:
            return [r for r in self.public_keys if not r.get("disabled") and use_l in (r.get("allowed_uses") or [])]
        except Exception:
            return []

    def get_ed25519_pubkeys_for_use(self, use: str) -> Dict[str, Ed25519PublicKey]:
        """Return mapping key_id -> Ed25519PublicKey for a specific use.

        Records missing key_id or with invalid key material are skipped.
        """
        result: Dict[str, Ed25519PublicKey] = {}
        for rec in self.get_public_keys_for_use(use):
            try:
                kid = rec.get("key_id")
                pub = self._load_ed25519_public_key(rec)
                if kid and pub:
                    result[str(kid)] = pub
            except Exception:
                continue
        return result

    def _load_ed25519_public_key(self, record: Dict[str, Any]) -> Optional[Ed25519PublicKey]:
        """Parse the record's public_key field into an Ed25519PublicKey.

        Supports:
            * Raw base64 of 32-byte key (prefixed optionally by 'base64:')
            * OpenSSH format lines starting with 'ssh-ed25519'
        """
        pk_field = record.get("public_key")
        if not pk_field or not isinstance(pk_field, str):
            return None
        pk_field = pk_field.strip()
        try:
            if pk_field.startswith("ssh-ed25519 "):
                # Full OpenSSH public key line
                bdata = pk_field.encode("utf-8")
                pub = serialization.load_ssh_public_key(bdata)
                if not isinstance(pub, Ed25519PublicKey):
                    return None
                return pub
            if pk_field.startswith("base64:"):
                pk_field = pk_field[len("base64:") :]
            raw = base64.b64decode(pk_field)
            if len(raw) != 32:
                return None
            return Ed25519PublicKey.from_public_bytes(raw)
        except Exception:
            return None

    def start_pubkey_challenge(self, username: str, key_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Initiate a public key authentication challenge.

        Returns mapping with keys: username, key_id, nonce_b64
        or None if user / key not found.
        """
        rec = self._resolve_public_key_record(username, key_id)
        if not rec:
            return None
        key_id_eff = rec.get("key_id") or "default"
        pub = self._load_ed25519_public_key(rec)
        if not pub:
            return None
        nonce_raw = secrets.token_bytes(32) + int(time.time()).to_bytes(8, "big")
        nonce_b64 = base64.b64encode(nonce_raw).decode()
        self._pk_challenges[(username, key_id_eff)] = {
            "nonce_raw": nonce_raw,
            "pubkey": pub,
            "expires_at": time.time() + 30,
        }
        return {"username": username, "key_id": key_id_eff, "nonce": nonce_b64}

    def verify_pubkey_response(self, username: str, key_id: str, signature_b64: str) -> bool:
        """Verify a client signature over the issued nonce.

        Args:
            username: User claiming identity.
            key_id: Key identifier used.
            signature_b64: Base64 encoded Ed25519 signature.
        """
        entry = self._pk_challenges.get((username, key_id))
        if not entry:
            return False
        if time.time() > entry.get("expires_at", 0):
            self._pk_challenges.pop((username, key_id), None)
            return False
        try:
            signature = base64.b64decode(signature_b64)
            pub: Ed25519PublicKey = entry["pubkey"]
            pub.verify(signature, entry["nonce_raw"])  # raises on failure
            # Success -> remove challenge
            self._pk_challenges.pop((username, key_id), None)
            return True
        except Exception:
            return False

    def authenticate_user(self, username: str, password: str) -> bool:
        """Authenticate a user by username + password.

        Convenience wrapper around :meth:`authenticate`.

        Args:
            username: Account identifier.
            password: Plain text candidate password.

        Returns:
            bool: True if credentials are valid; False otherwise.
        """
        # This is a more explicitly named version of the authenticate method
        return self.authenticate(username, password)

    def authenticate_key(self, api_key: str) -> bool:
        """Authenticate an API key.

        Convenience wrapper around :meth:`verify_api_key`.

        Args:
            api_key: Full API key token supplied by client.

        Returns:
            bool: True if key exists; False otherwise.
        """
        # This is a more explicitly named version of the verify_api_key method
        return self.verify_api_key(api_key)

    def get_key_permissions(self, api_key: str) -> Optional[str]:
        """Return permissions associated with an API key.

        Alias for :meth:`get_api_key_permissions`.

        Args:
            api_key: API key token.

        Returns:
            str | None: Permission string (e.g. "read-only", "read-write",
            custom value) or None if key not found.
        """
        return self.get_api_key_permissions(api_key)

    def authenticate(self, username: str, password: str) -> bool:
        """Core username/password authentication flow with caching.

        Applies a 5-minute positive/negative result cache keyed by the exact
        username + password to avoid repeated SHA-256 hashing for high-volume
        attempts.

        Args:
            username: Account identifier.
            password: Plain text password.

        Returns:
            bool: True if credentials match a configured user; False otherwise.
        """
        # Check cache first
        cache_key = f"{username}:{password}"
        if cache_key in self.auth_cache:
            # Cache entry expires after 5 minutes
            if time.time() - self.auth_cache[cache_key]["time"] < 300:
                return self.auth_cache[cache_key]["result"]

        # Not in cache or expired, check authentication
        result = False

        # 1) Local static users (if present)
        if "users" in self.config:
            for user in self.config["users"]:
                if user.get("username") == username:
                    # Check password hash
                    if self._verify_password(password, user.get("password_hash", "")):
                        result = True
                        break

        # 2) PAM provider (optional) if not matched by local users
        if not result and self._pam_enabled:
            result = self._pam_authenticate(username, password)

        # Update cache
        self.auth_cache[cache_key] = {"result": result, "time": time.time()}

        return result

    def verify_api_key(self, api_key: str) -> bool:
        """Check if an API key is configured.

        Args:
            api_key: API key token.

        Returns:
            bool: True if present in configuration; False otherwise.
        """
        if "api_keys" not in self.config:
            return False

        for key_entry in self.config["api_keys"]:
            if key_entry["key"] == api_key:
                return True

        return False

    def get_user_permissions(self, username: str) -> Optional[str]:
        """Resolve permissions for a configured user.

        Precedence:
            1. Explicit "permissions" field (static users)
            2. If is_admin True -> "admin" (static users)
            3. PAM group mapping if PAM enabled (admin > write > read)
            4. Default -> "read-write"

        Args:
            username: Account identifier.

        Returns:
            str | None: Permissions string or None if user not found.
        """
        # Static users block takes precedence when present
        if "users" in self.config:
            for user in self.config["users"]:
                if user.get("username") == username:
                    # Explicit permissions field
                    if "permissions" in user:
                        return user["permissions"]
                    # Backward-compat admin toggle
                    if user.get("is_admin", False):
                        return "admin"
                    # Fall through to default if static user matched
                    return "read-write"

        # If PAM is enabled, map groups to permissions
        if self._pam_enabled:
            perms = self._pam_group_permissions(username)
            if perms:
                return perms

        # Unknown user: if this is reached, the caller likely failed auth.
        return None

    # =================== PAM Backend ===================
    def _pam_authenticate(self, username: str, password: str) -> bool:
        """Authenticate a user against the system PAM service.
        
        Applies policy controls: allow_root (default False) and optional allowed_users.
        Returns False gracefully if PAM libraries are unavailable.
        """
        # Policy checks before invoking PAM
        if (not getattr(self, "_pam_allow_root", False)) and username == "root":
            return False
        if self._pam_allowed_users is not None and username not in self._pam_allowed_users:
            return False
        try:
            # Try python-pam (module name: pam)
            import pam  # type: ignore

            p = pam.pam()
            ok = bool(p.authenticate(username, password, service=self._pam_service))
            if not ok:
                # Optional: log minimal reason without sensitive info
                self.logger.debug("PAM auth failed for user '%s' (service=%s)", username, self._pam_service)
            return ok
        except Exception as e:  # pragma: no cover - environment-dependent
            # PAM not available or error during auth; do not crash
            self.logger.warning("PAM authentication unavailable or failed: %s", e)
            return False

    def _pam_group_permissions(self, username: str) -> Optional[str]:
        """Determine permissions based on system group membership.

        Mapping precedence: admin_group -> "admin"; write_group -> "read-write"; read_group -> "read-only".
        Returns None if user not resolvable or no matching groups found.
        """
        try:
            import grp
            import pwd

            # Resolve user's primary group name
            try:
                pw = pwd.getpwnam(username)
                primary_gid = pw.pw_gid
                primary_group = grp.getgrgid(primary_gid).gr_name if primary_gid is not None else None
            except KeyError:
                primary_group = None

            # Collect all groups where user is a member
            groups = set()
            if primary_group:
                groups.add(primary_group)
            for g in grp.getgrall():
                if username in (g.gr_mem or []):
                    groups.add(g.gr_name)

            admin_g = self._pam_groups.get("admin_group")
            write_g = self._pam_groups.get("write_group")
            read_g = self._pam_groups.get("read_group")

            if admin_g and admin_g in groups:
                return "admin"
            if write_g and write_g in groups:
                return "read-write"
            if read_g and read_g in groups:
                return "read-only"
            return None
        except Exception as e:  # pragma: no cover - environment-dependent
            self.logger.debug("PAM group lookup failed for '%s': %s", username, e)
            return None

    def get_api_key_permissions(self, api_key: str) -> Optional[str]:
        """Return permissions associated with an API key.

        Args:
            api_key: API key token.

        Returns:
            str | None: Permissions (defaults to "read-only") or None if key
            not found.
        """
        if "api_keys" not in self.config:
            return None

        for key_entry in self.config["api_keys"]:
            if key_entry["key"] == api_key:
                return key_entry.get("permissions", "read-only")

        return None

    def _verify_password(self, password: str, stored_hash: str) -> bool:
        """Compare plaintext password to stored SHA-256 hash.

        Args:
            password: Candidate plaintext password.
            stored_hash: Expected hex SHA-256 digest.

        Returns:
            bool: True if the computed hash matches; False otherwise.
        """
        # Calculate hash of the provided password
        password_hash = hashlib.sha256(password.encode()).hexdigest()

        # Compare with stored hash using constant-time comparison
        return secrets.compare_digest(password_hash, stored_hash)

    def hash_password(self, password: str) -> str:
        """Return hex SHA-256 digest for a password.

        Args:
            password: Plain text password.

        Returns:
            str: Hex-encoded SHA-256 digest.
        """
        return hashlib.sha256(password.encode()).hexdigest()

    def generate_api_key(self) -> str:
        """Generate a cryptographically random API key.

        Returns:
            str: 32-character hex token (128 bits entropy).
        """
        return secrets.token_hex(16)
