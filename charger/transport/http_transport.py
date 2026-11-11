import json
import urllib.error
import urllib.request

from .base import Transport, TransportError


class HttpTransport(Transport):
    """Wi-Fi / cellular. Add LoRa, BLE-to-phone, satellite as sibling classes."""

    def __init__(self, base_url: str, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send(self, batch):
        req = urllib.request.Request(
            self.base_url + "/sync", data=json.dumps({"events": batch}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise TransportError(str(ex)) from ex


class SimulatedLink(Transport):
    """Wraps a real transport with an on/off switch to simulate losing coverage."""

    def __init__(self, inner: Transport, online: bool = True):
        self.inner = inner
        self.online = online

    def send(self, batch):
        if not self.online:
            raise TransportError("link down (simulated)")
        return self.inner.send(batch)
