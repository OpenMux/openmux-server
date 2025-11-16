# Contributing to OpenMux

Thank you for your interest in contributing to OpenMux! This document captures the core contribution guidelines with a focus on the recently established Exception Handling & Logging Policy to maintain observability and debuggability.

## Development Workflow (Short Form)
1. Fork / feature branch naming: `feature/<short-description>` or `fix/<issue-id>`.
2. Keep changes focused; unrelated refactors should be separate PRs.
3. Run tests and lint before submitting.
4. Provide context in PR description: problem statement, approach, follow-ups.
5. Prefer small, reviewable commits with clear messages.

## Code Style
- Follow existing code patterns for async structure, logging, and adapter abstractions.
- Avoid introducing blocking calls in async paths.
- Prefer explicit `asyncio.create_task()` over spawning unnoticed background coroutines.
- Keep functions cohesive; extract helpers when complexity grows.

## Exception Handling & Logging Policy
Robust error visibility is critical. Broad catch blocks (`except Exception`) may still exist but must obey one of the two allowed patterns:

### 1. Logged Operational Error (Preferred)
Use this when the exception indicates a real fault, protocol anomaly, I/O failure, or anything that could affect state, correctness, or user experience.

```python
try:
    await adapter.send_data(data)
except Exception as e:
    logger.error("Failed to send data: %s", e, exc_info=True)
    return False
```

Requirements:
- Use `logger.error()` (or `warning`/`info` if genuinely lower severity) with `exc_info=True`.
- Provide clear message context (what failed, not just "error").
- Downstream state adjustment (e.g. marking disconnected) should happen inside the handler if needed.

### 2. Justified Silent / Non-Logging Block (Rare)
Permitted only for best-effort, non-critical, *expected* failure paths where logging would create noise or duplicate prior logging.

These must include an inline justification comment beginning with the literal text `justification:`:

```python
try:
    self.last_port_metadata = list(dedup.values())
except Exception:  # justification: optional metadata; failure does not affect functional port list
    pass
```

Acceptable justification categories:
- Optional metadata or cosmetic enhancement
- Heuristic validation (treat as invalid on failure)
- Redundant best-effort cleanup during shutdown
- Legacy/probe fallback where outer path already logs failure
- Idempotent safety guard (state flag, attribute injection)

Not acceptable:
- Swallowing protocol, transport, authentication, parsing, or persistence errors
- Using silence to hide noisy but fixable bugs

### Narrowing Exceptions
Where practical, prefer specific exceptions (e.g. `asyncio.TimeoutError`, `json.JSONDecodeError`, `OSError`). Broad handlers exist mainly where many error types map to a single recovery path.

### Escalation & Re-Raising
If a handler cannot make progress and higher layers must decide, prefer re-raising after logging:
```python
except SpecificError as e:
    logger.error("Parser failed for control frame: %s", e, exc_info=True)
    raise
```


## Logging Practices
- Use structured context when helpful: `logger.error("Failed auth for user %s", username, exc_info=True)`
- Avoid logging secrets (passwords, API keys).
- Do not log per-byte high-frequency data at error level; use debug.

## Tests
- Add regression tests for protocol-level changes.
- For new adapters, include connection + basic lifecycle tests (connect/auth/list/connect-port/send/read/close).


## Contributor License Terms (No License Yet)

The project has not selected a final license yet ("No license yet"). To keep future licensing options open, all contributions must be license-flexible and free of additional restrictions.

By submitting a contribution (code, docs, tests, or otherwise), you agree to the following:

- Rights and originality: You have the necessary rights to contribute the material or have obtained permission to do so, and you identify any third-party code/data and its license in your PR.
- Broad copyright grant: You grant the project maintainers a perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to use, reproduce, modify, adapt, publicly perform, publicly display, distribute, sublicense, and relicense your contribution as part of this project.
- Relicensing: You expressly permit the project to relicense your contribution, in whole or in part, under any OSI-approved open-source license (including permissive or copyleft licenses) chosen now or in the future.
- Patent grant: To the extent you have patent rights that are necessarily infringed by your contribution, you grant a perpetual, worldwide, non-exclusive, royalty-free, irrevocable patent license to practice, make, use, sell, offer to sell, import, and otherwise transfer your contribution as part of this project and its derivatives.
- No additional restrictions: You will not impose any further terms or conditions on your contribution (no special terms that would limit the project’s ability to choose a future license).

If your contribution includes third-party material, ensure its license is compatible with broad relicensing and include required notices/attributions.

### Developer Certificate of Origin (DCO)

By contributing, you certify the Developer Certificate of Origin (https://developercertificate.org/). Please sign your commits:

```
Signed-off-by: Your Name <you@example.com>
```

This affirms you have the right to submit the work under the terms above and that no additional restrictions are attached.
