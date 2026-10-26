# 1. The server owns the wording of every access decision

Date: 2026-10-26. Status: accepted. Relates to GitHub issue #98 and
`docs/design/architecture-review-2026-10-25.md` section 2.

## Context

Every answer about the Write slot reached a Client as a small control frame. The frame carried a status word, and each
surface turned that word into English on its own. The TCP client, the Telnet and SSH listeners, and the web console each
held a table for the same words. The tables drifted: a Port with `max_read_write_users: 0` was reported as
`promote_failed`, which reads as a server fault, and one refusal printed a sentence in one surface and nothing in
another. Adding one outcome meant editing four decoders, and a Client that missed an entry stayed silent.

## Decision

The server builds the sentence. `openmux/server/access_decision.py` holds one row per outcome code: its kind, whether it
is a grant, and the lines a surface shows. A producer calls `decide()` or `notice()` and renders `decision.frame()`. No
client, and no web script, maps a code to text.

The frame keeps its name `client_mode` and keeps `mode`, so a surface still updates its state from `mode` and `ok`. It
reports every access decision, not only a mode change, and carries:

- `code`: the outcome code, for state only. A surface may hide a menu on `granted`; it must not build a sentence from it.
- `lines`: the sentences to show, already bracketed. An empty list means "change your state, show nothing".
- `message`: one line for a log or a close reason, capped at `MESSAGE_LIMIT`.

The frame no longer carries `reason`, `takeover`, or `taken_by`. The viewer list no longer rides on `client_mode`: it
travels in its own `presence` frame, because a viewer badge is not a mode change.

## Consequences

A new outcome needs one row, not four decoders. A surface cannot forget a case, and a Client never guesses.

The change is visible on the wire, and an older Client cannot read the new sentences:

- A 1.0.5 server with a 1.0.4 or older Client: the Client still sets its mode, because `mode` and `ok` are unchanged, and
  it keeps its own wording for the codes it knows. It shows nothing where it used to read `takeover` or `taken_by`, and
  nothing for a code it has never seen. Upgrade the Client with the server to keep the sentences.
- A 1.0.5 Client with a server older than 1.0.5: no `code` or `lines` arrive, so the Client prints nothing. The Client
  does not fall back to invented wording, because inventing wording is the fault this decision removes. Upgrade the
  server with the Client.

Federated nodes do not forward these frames to Clients. A local Client's frame, and the mirror of a federated holder on
the peer node, both come from the ConsoleManager on the node that serves that Client, so each node renders its own
wording. A MuxCon link carries `FEDRWACK:<port>:<stream>:<mode>`, which is a result, not a sentence. A mixed-version
link therefore needs no compatibility window, and no config key changed.
