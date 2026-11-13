"""Control signals for interrupting a running agent turn.

A human interjection is not a queued suggestion: when the user types while an
agent is answering, the current turn must stop and the same agent must answer
again with the note appended.  Runtimes raise :class:`AgentInterrupted` from
their streaming loop when the pipeline's per-task event is set, then the
pipeline restarts that member's session and reruns the turn.
"""

from __future__ import annotations


class AgentInterrupted(Exception):
    """The current turn was interrupted by a human interjection."""
