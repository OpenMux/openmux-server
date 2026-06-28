"""Authentication management utilities for the OpenMux server.

Provides a lightweight credential + API key verification layer with:
* Password hashing / comparison (SHA-256, constant-time compare)
* Short‑term authentication result caching to reduce hashing cost
* API key permission lookups
* External authentication via a helper binary (e.g. openmux-pam-helper)
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

from .security_policy import SecurityPolicy


class AuthManager:
    def __init__(self, config: Dict[str, Any], security_policy: Optional[SecurityPolicy] = None):
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
        # External authentication via helper binary (config key: external_auth)
        # Deprecated alias: pam (mapped automatically with a warning)
        self._load_ext_auth_config(config)
        # Active pubkey challenges: (username, key_id) -> {nonce_raw, pubkey, expires_at}
        self._pk_challenges = {}
        # Active password HMAC challenges: username -> {nonce_raw, expires_at, pw_hex}
        self._pw_hmac_challenges = {}
        # Authentication failure tracking: (username, ip) -> {failures, first_failure, locked_until}
        self._fail_tracker = {}
        # Policy parameters (overridable via security policy)
        self._max_fail_window = 300  # seconds window for counting failures
        self._base_lock_seconds = 30  # initial lock duration after threshold
        self._failure_threshold = 5  # failures before lock engages
        self._security_policy = security_policy
        self._apply_security_policy(security_policy)

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
        # Reload external auth config (supports both external_auth: and deprecated pam: key)
        self._load_ext_auth_config(new_config)
        # Clear cache when config changes
        self.auth_cache = {}
        self._ext_auth_groups_cache.clear()
        # Clear outstanding challenges (they reference old keys)
        self._pk_challenges.clear()
        self._pw_hmac_challenges.clear()
        # Re-apply security policy in case rate limits depend on config
        self._apply_security_policy(self._security_policy)

    def update_security_policy(self, policy: Optional[SecurityPolicy]) -> None:
        """Apply a new security policy at runtime."""

        self._security_policy = policy
        self._apply_security_policy(policy)

    def _apply_security_policy(self, policy: Optional[SecurityPolicy]) -> None:
        if not policy:
            return
        limits = policy.get_auth_rate_limits()
        window = limits.get("window_seconds")
        threshold = limits.get("failure_threshold")
        base = limits.get("base_lock_seconds")
        if isinstance(window, int) and window > 0:
            self._max_fail_window = window
        if isinstance(threshold, int) and threshold > 0:
            self._failure_threshold = threshold
        if isinstance(base, int) and base > 0:
            self._base_lock_seconds = base

    def _load_ext_auth_config(self, config: Dict[str, Any]) -> None:
        """Populate external-auth fields from config.

        Reads ``external_auth:`` first. If absent, falls back to the
        deprecated ``pam:`` key and emits a one-time warning so operators
        know to migrate their config.
        """
        ext_cfg = config.get("external_auth")
        if ext_cfg is None:
            # Deprecated alias: pam -> external_auth
            pam_cfg = config.get("pam")
            if pam_cfg:
                self.logger.warning(
                    "Config key 'pam' is deprecated; rename to 'external_auth'. "
                    "Also rename 'service_name' to 'service'."
                )
                # Map old pam keys to new external_auth shape
                ext_cfg = dict(pam_cfg)
                if "service_name" in ext_cfg and "service" not in ext_cfg:
                    ext_cfg["service"] = ext_cfg.pop("service_name")
            else:
                ext_cfg = {}

        ext_cfg = ext_cfg or {}
        self._ext_auth_enabled: bool = bool(ext_cfg.get("enabled", False))
        self._ext_auth_service: str = str(ext_cfg.get("service", "openmux"))
        # helper may be a string (path to execute) or a list (passed directly to subprocess)
        self._ext_auth_helper = ext_cfg.get("helper")  # str | list | None
        self._ext_auth_timeout: float = float(ext_cfg.get("timeout", 10))
        self._ext_auth_allow_root: bool = bool(ext_cfg.get("allow_root", False))
        allowed_users_cfg = ext_cfg.get("allowed_users")
        self._ext_auth_allowed_users = set(allowed_users_cfg) if isinstance(allowed_users_cfg, list) else None
        groups_cfg = ext_cfg.get("groups") or {}
        self._ext_auth_groups: Dict[str, str] = {
            "admin_group": str(groups_cfg.get("admin_group", "openmux_admin")),
            "write_group": str(groups_cfg.get("write_group", "openmux_write")),
            "read_group": str(groups_cfg.get("read_group", "openmux_read")),
        }
        # Fallback permission when a user authenticates but no group mapping resolves.
        # Set to e.g. "read-write" to grant all external-auth users a default role.
        raw_default = ext_cfg.get("default_permission")
        self._ext_auth_default_permission: Optional[str] = str(raw_default) if raw_default else None
        # Short-lived cache of groups returned by the helper JSON (keyed by username).
        # Populated on successful auth; entries expire after 5 minutes (matching auth cache TTL).
        if not hasattr(self, "_ext_auth_groups_cache"):
            self._ext_auth_groups_cache: Dict[str, Any] = {}

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

        # 2) External auth provider (optional) if not matched by local users
        if not result and self._ext_auth_enabled:
            result = self._external_authenticate(username, password)

        # Update cache (only cache successes to avoid silencing transient failures or locking out retries)
        if result:
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
            2. PAM group mapping if PAM enabled (admin > write > read)
            3. Default -> "read-write"

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
                    # Fall through to default if static user matched
                    return "read-write"

        # If external auth is enabled, map groups to permissions
        if self._ext_auth_enabled:
            perms = self._ext_auth_group_permissions(username)
            if perms:
                return perms

        # Unknown user: if this is reached, the caller likely failed auth.
        return None

    # =================== External Auth Backend ===================
    def _external_authenticate(self, username: str, password: str) -> bool:
        """Authenticate a user via the configured external auth helper.

        Protocol (matches openmux-pam-helper)::

            stdin line 1: username
            stdin line 2: password

        No arguments beyond the helper binary itself (and an optional leading
        ``sudo`` when the helper path is configured as e.g.
        ``sudo /usr/local/bin/openmux_pam_helper``).

        Service name is passed as an optional first argument so the helper
        can select the correct PAM service::

            helper [<service>]
            stdin line 1: username
            stdin line 2: password

        Exit code 0 = success.  Stdout JSON (``{"ok": true, ...}``) is
        parsed to confirm the ``ok`` field; a missing or malformed JSON
        object is treated as a failure.

        Helper path is resolved in order:
        1. ``external_auth.helper`` config value (supports ``sudo /path`` form)
        2. ``openmux-pam-helper`` / ``openmux_pam_helper`` on PATH
        3. ``/usr/local/bin/openmux_pam_helper``
        """
        import json as _json
        import os
        import shutil
        import subprocess

        # Policy checks
        if not self._ext_auth_allow_root and username == "root":
            self.logger.warning("External auth denied: root login disabled by policy")
            return False
        if self._ext_auth_allowed_users is not None and username not in self._ext_auth_allowed_users:
            self.logger.warning("External auth denied: user '%s' not in allowed_users list", username)
            return False

        helper = self._ext_auth_helper

        if isinstance(helper, list) and helper:
            # List config e.g. ["sudo", "/usr/local/bin/openmux_pam_helper"]
            cmd = list(helper)
            # Binary for existence check: last absolute path in the list
            binary = next((c for c in reversed(cmd) if os.path.isabs(c)), cmd[-1])
        elif isinstance(helper, str) and helper.strip():
            # String config: the string itself is the binary to execute
            cmd = [helper.strip()]
            binary = helper.strip()
        else:
            # Auto-resolve from PATH then well-known location
            found = shutil.which("openmux-pam-helper") or shutil.which("openmux_pam_helper")
            binary = found or "/usr/local/bin/openmux_pam_helper"
            cmd = [binary]

        # Append service name as optional argument
        if self._ext_auth_service:
            cmd.append(self._ext_auth_service)

        if not os.path.isfile(binary) and not shutil.which(binary):
            self.logger.error("External auth helper not found: %s", binary)
            return False

        try:
            proc = subprocess.run(
                cmd,
                input=f"{username}\n{password}\n".encode("utf-8"),
                capture_output=True,
                timeout=self._ext_auth_timeout,
            )
            ok = proc.returncode == 0
            # Cross-check JSON ok field when present; also cache groups for permission lookup
            if proc.stdout:
                try:
                    data = _json.loads(proc.stdout.decode("utf-8", errors="replace").strip())
                    ok = ok and bool(data.get("ok", True))
                    if ok and isinstance(data.get("groups"), list):
                        self._ext_auth_groups_cache[username] = {
                            "groups": data["groups"],
                            "expires": time.time() + 300,
                        }
                except Exception:
                    pass
            if not ok:
                stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()
                self.logger.warning(
                    "External auth failed for user '%s' (exit=%d%s)",
                    username,
                    proc.returncode,
                    f": {stderr_msg}" if stderr_msg else "",
                )
            else:
                self.logger.debug("External auth succeeded for user '%s'", username)
            return ok
        except subprocess.TimeoutExpired:
            self.logger.error(
                "External auth helper timed out after %ss for user '%s'",
                self._ext_auth_timeout,
                username,
            )
            return False
        except Exception as e:
            self.logger.error("External auth helper error for user '%s': %s", username, e)
            return False

    def _ext_auth_group_permissions(self, username: str) -> Optional[str]:
        """Determine permissions based on group membership.

        Checks (in order):
        1. Groups cached from the last helper JSON response for this user.
        2. Unix system group membership (grp/pwd).
        3. default_permission fallback if configured.

        Mapping precedence: admin_group -> "admin"; write_group -> "read-write";
        read_group -> "read-only". Returns None if no matching group found.
        """
        import time as _time

        admin_g = self._ext_auth_groups.get("admin_group")
        write_g = self._ext_auth_groups.get("write_group")
        read_g = self._ext_auth_groups.get("read_group")

        def _map_groups(groups: set) -> Optional[str]:
            if admin_g and admin_g in groups:
                return "admin"
            if write_g and write_g in groups:
                return "read-write"
            if read_g and read_g in groups:
                return "read-only"
            return None

        # 1. Groups from helper JSON (most authoritative for external users)
        cached = self._ext_auth_groups_cache.get(username)
        if cached and _time.time() < cached.get("expires", 0):
            result = _map_groups(set(cached["groups"]))
            if result:
                return result

        # 2. Unix group membership
        try:
            import grp
            import pwd

            try:
                pw = pwd.getpwnam(username)
                primary_gid = pw.pw_gid
                primary_group = grp.getgrgid(primary_gid).gr_name if primary_gid is not None else None
            except KeyError:
                primary_group = None

            groups: set = set()
            if primary_group:
                groups.add(primary_group)
            for g in grp.getgrall():
                if username in (g.gr_mem or []):
                    groups.add(g.gr_name)

            result = _map_groups(groups)
            if result:
                return result
        except Exception as e:  # pragma: no cover - environment-dependent
            self.logger.debug("Group lookup failed for '%s': %s", username, e)

        # 3. Configured default for external-auth users
        return self._ext_auth_default_permission
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
