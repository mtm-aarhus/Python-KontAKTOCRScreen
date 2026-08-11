"""This module handles resetting the state of the computer so the robot can work with a clean slate.

For this robot the "state" is just the cached KontAKT credentials. ``open_all``
caches them and returns a :class:`Client`; ``reset`` re-reads them, so the queue
framework can reconnect on a retry. Documents are read from KontAKT's local file
store over the API, so no external connection is needed.
"""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection


class Client:
    """Cached KontAKT credentials, read by ``open_all`` and reused across every
    queue element (a run screens many documents)."""

    def __init__(self, orchestrator_connection: OrchestratorConnection):
        kontakt = orchestrator_connection.get_credential("KontAKTAPI")
        self.kontakt_base = kontakt.username
        self.kontakt_key = kontakt.password
        # Screening rules per sag, for this run. The rules belong to the sag, not
        # to the document, so a 200-document sag asks once instead of 200 times.
        self.screening_specs: dict = {}


def reset(orchestrator_connection: OrchestratorConnection) -> Client:
    """Clean up, close/kill all programs, then (re)open the connections.

    Returns the freshly-opened :class:`Client` so the queue framework can reuse
    it across queue elements (and reconnect by calling ``reset`` again)."""
    orchestrator_connection.log_trace("Resetting.")
    clean_up(orchestrator_connection)
    close_all(orchestrator_connection)
    kill_all(orchestrator_connection)
    return open_all(orchestrator_connection)


def clean_up(orchestrator_connection: OrchestratorConnection) -> None:
    """Do any cleanup needed to leave a blank slate."""
    orchestrator_connection.log_trace("Doing cleanup.")


def close_all(orchestrator_connection: OrchestratorConnection) -> None:
    """Gracefully close all applications used by the robot."""
    orchestrator_connection.log_trace("Closing all applications.")


def kill_all(orchestrator_connection: OrchestratorConnection) -> None:
    """Forcefully close all applications used by the robot."""
    orchestrator_connection.log_trace("Killing all applications.")


def open_all(orchestrator_connection: OrchestratorConnection) -> Client:
    """Open all connections used by the robot and return them as a :class:`Client`."""
    orchestrator_connection.log_trace("Reading KontAKT credentials.")
    return Client(orchestrator_connection)
