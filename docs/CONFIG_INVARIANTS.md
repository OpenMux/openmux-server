# Configuration Invariants

This document defines non-negotiable invariants for the OpenMux server configuration format so future refactors cannot silently break or coerce user configs.

## 1. Canonical Sections (Primary Format)
Adapters are configured via top-level section keys. These keys are the *source of truth*:
`server`, `authentication`, `logging`, `client_listener`, `serial_ports`, `loopback_ports`, `command_ports`, `tcp_initiator_ports`, `openmux_client_ports`, `muxcon`, `web_console`, and `web_status`.
New sections MUST be documented here before being accepted. Sidecar files (see §7) still materialize in-memory under these canonical keys.


## 2. No Implicit Transformation
The factory MUST NOT rewrite, remove, wrap, or rename user-provided sections. Loading is read-only.

## 3. Stable Mapping
Adapter class resolution uses explicit plugin metadata (`AdapterPlugin.config_section`). No heuristic string slicing (e.g. stripping `_ports`) is allowed for deriving adapter types.

## 4. Deprecation Policy
Deprecation warnings for config keys require ALL of:
1. Approved migration plan documented in this file (append a dated section).
2. At least one release cycle of dual support.
3. Tests asserting both old and new forms produce identical runtime adapter sets.

## 5. Logging Discipline
- The factory MUST NOT emit warnings for valid canonical sections.
- A warning MUST contain a link or reference to a migration section if (and only if) a deprecation is active.

## 6. Test Guard (Required)
Add (and maintain) a test that:
1. Supplies only canonical sections and asserts all expected adapters instantiate.
2. Captures log output and asserts zero WARNING lines referencing deprecation.
3. Avoids unified list formats; tests cover per‑section schemas only.

## 7. Sidecar Configuration Files (authentication/security)
The primary config (`server.yaml`) may reference two sidecar files: `authentication.yaml` and `security.yaml`.

- `authentication.yaml` contains only the `authentication` mapping (users, API keys, public keys, PAM). When externalized, `server.yaml` MUST omit inline credentials, and ConfigManager MUST keep the merged runtime view consistent for consumers.
- `security.yaml` defines the adapter allow-list (`allowed_modules`, `allowed_adapter_types`), `config_editor.writable_sections`, authentication rate-limit overrides, and (optionally) the drop-to-user policy for the Command adapter. `block_unlisted` defaults to true; disabling it must be an explicit, documented choice.
- CLI flags `--auth-config` and `--security-config` MUST remain available so deployments can relocate sidecars. Defaults derive from the directory containing `server.yaml`.
- Config Editor and hot-reload operations MUST honor `config_editor.writable_sections`. Empty list => read-only UI; absence of the block => legacy editable behavior.

Any future sidecar files (e.g., secrets) require expanding this section with invariants before implementation.

## 8. Backward Compatibility Window
If a new format is introduced, the old format stays functional for a minimum of two minor versions unless a security issue mandates earlier removal. Such exceptions must be documented.

## 9. Documentation Synchronization
`README.md` and configuration examples under `docs/configuration/` MUST match these invariants. Pull requests altering config parsing MUST update examples in the same commit.

## 10. Change Control Checklist
Any PR affecting configuration parsing MUST include a checklist in its description:
- [ ] Maintains canonical section acceptance
- [ ] Does not auto-generate or mutate user config
- [ ] Adds/updates tests for new parsing behavior
- [ ] Updates docs/examples
- [ ] Adds/updates migration notes (if deprecating)
- [ ] Verified no new warnings for existing sample configs

## 11. Unknown Top-Level Sections
Any top-level key not in the canonical list (or an explicitly documented optional key) is a configuration error and MUST abort startup with a clear message listing the unknown keys. This catches typos early. No auto-ignore, no fallback.

## 12. Environment Variable Expansion
Automatic `${VAR}` interpolation is NOT performed globally. Only explicitly whitelisted boolean/debug toggles MAY consult environment variables (e.g. a `DEBUG` mode) and must be documented. All other literal `${...}` strings are treated as plain text. Introducing general env expansion requires an update to this file and an opt-in toggle.

## 13. Ordering Guarantees
Parsing is order-independent. Adapter instantiation order MUST be deterministic: sort by adapter type key, then by declared port/name within that section. Tests SHOULD assert determinism to prevent flaky comparisons or hash/order dependent bugs.

## 14. Duplicate Name Handling
Local adapter/port names MUST be unique across all locally defined sections; duplicates cause a startup error with both source sections identified. Federated/muxcon obtained remote ports MAY collide in name with local ones but MUST be namespaced or otherwise disambiguated at runtime (e.g. `remote:<origin>:<name>` or an internal unique identifier) to prevent cross-talk. No silent override of a local definition by a federated one.

## 15. TLS / Security Invariants
If `tls.enabled` (or equivalent) is true for a section:
- Required cert/key parameters MUST be present; absence is a startup error.
- There is NO silent downgrade to plaintext.
- Partial specification (e.g. cert without key) aborts with a clear diagnostic.
- Explicit plaintext operation only occurs when `tls.enabled` is false or omitted.

## 16. Forward Compatibility of Nested Keys
Within a known section, unknown nested keys MAY be ignored but only emit a DEBUG-level log (never WARNING/ERROR) unless a security-related key is malformed. This allows forward extension without breaking older binaries while keeping noise low.

## 17. Include / Import Mechanisms
Implicit multi-file include/import features are DISALLOWED. YAML anchors & aliases are permitted as inherent YAML features but MUST NOT be relied upon by core logic beyond standard parsing. Adding an explicit `include:` or similar directive requires a new invariant section, tests, and security review (path traversal, recursion limits).

---
*Last updated: 2025-08-24*
