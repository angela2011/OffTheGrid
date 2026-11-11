"""Drains the outbox over any Transport. Knows nothing about OCPP or payments."""
import threading

from charger.transport.base import TransportError


class Sender:
    def __init__(self, outbox, transport, batch_size=50):
        self.outbox = outbox
        self.transport = transport
        self.batch_size = batch_size

    def on_link_up(self):
        self.outbox.clear_backoff()

    def drain_once(self) -> dict:
        batch = self.outbox.claim_batch(self.batch_size)
        if not batch:
            return {"sent": 0, "acked": 0, "rejected": 0, "error": None}
        for e in batch:
            e["charger_id"] = self.outbox.charger_id
        ids = [e["event_id"] for e in batch]
        try:
            resp = self.transport.send(batch)
        except TransportError as ex:
            self.outbox.release_failed(ids, str(ex))
            return {"sent": len(batch), "acked": 0, "rejected": 0, "error": str(ex)}
        acked = [i for i in ids if i in set(resp.get("acked", []))]
        rejected = resp.get("rejected", {})
        self.outbox.mark_acked(acked)
        for eid, reason in rejected.items():
            self.outbox.mark_rejected(eid, reason)
        unanswered = [i for i in ids if i not in acked and i not in rejected]
        if unanswered:
            self.outbox.release_failed(unanswered, "no_ack")
        return {"sent": len(batch), "acked": len(acked), "rejected": len(rejected), "error": None}

    def drain_all(self, max_rounds=1000) -> dict:
        total = {"acked": 0, "rejected": 0, "error": None}
        for _ in range(max_rounds):
            r = self.drain_once()
            total["acked"] += r["acked"]
            total["rejected"] += r["rejected"]
            if r["error"]:
                total["error"] = r["error"]
                break
            if r["sent"] == 0:
                break
        return total

    def run_forever(self, stop: threading.Event, interval=2.0):
        """For a background thread. Give it its own Outbox/connection on the same DB file."""
        while not stop.is_set():
            self.drain_once()
            stop.wait(interval)
