"""Codex reviewer providers.

Two transports, both selectable, neither replacing the other:

* `conversation.py` + `quota.py` + `reply.py` — `codex_cli`, one `codex exec`
  process per turn. Stateless between turns, so it cannot chunk. It reads both
  of its streams as decorated TEXT, which is why two of its three modules are
  guards on that text: `quota.py` keeps the loop's OWN prompt out of the failure
  evidence (`codex exec` echoes the whole prompt onto stderr), and `reply.py`
  isolates the reviewer's message out of a stdout transcript that also carries
  the echoed prompt, hook lines, a token counter and a duplicate of the answer.
* `sandbox.py` + `preflight.py` — whether selecting `codex_cli` is SAFE, and
  whether it would work. `sandbox.py` reads `codex.sandbox_args` as a policy and
  refuses an unconfined one; `preflight.py` resolves the working directory both
  transports share and makes one trivial invocation with those flags, so a seat
  that cannot review fails before a round rather than during one.
* `app_server.py` + `app_server_conversation.py` + `wire.py` +
  `protocol_errors.py` — `codex_app_server`, one `codex app-server` process
  holding one thread. It can chunk, and classifies failures from protocol
  fields rather than from stderr text.
"""
