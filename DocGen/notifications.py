# pyright: reportAttributeAccessIssue=false

import json
from urllib.request import Request, urlopen

from django.conf import settings


class BaseNotificationAdapter:
    def publish(self, event_type: str, payload: dict) -> bool:
        raise NotImplementedError()


class LocalNotificationAdapter(BaseNotificationAdapter):
    def publish(self, event_type: str, payload: dict) -> bool:
        _ = (event_type, payload)
        return True


class HttpNotificationAdapter(BaseNotificationAdapter):
    def __init__(self):
        self.endpoint = getattr(settings, "DOCGEN_NOTIFICATION_HTTP_ENDPOINT", "")
        self.timeout_seconds = float(getattr(settings, "DOCGEN_NOTIFICATION_TIMEOUT_SECONDS", 3.0))
        self.api_token = getattr(settings, "DOCGEN_NOTIFICATION_API_TOKEN", "")

    def publish(self, event_type: str, payload: dict) -> bool:
        if not self.endpoint:
            return False

        body = {
            "event_type": event_type,
            "payload": payload,
        }
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        request = Request(self.endpoint, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", 200)
        except Exception:
            return False

        return int(status) < 400
