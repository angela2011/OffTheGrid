from abc import ABC, abstractmethod


class TransportError(Exception):
    """Link down / timeout / server unreachable. Always retryable."""


class Transport(ABC):
    @abstractmethod
    def send(self, batch: list) -> dict:
        """Deliver events. Return {"acked": [event_ids], "rejected": {event_id: reason}}.
        Raise TransportError if the link is unavailable."""
