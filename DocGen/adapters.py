# pyright: reportAttributeAccessIssue=false

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings


class BaseActorResolutionAdapter:
    def resolve(self, actor_type: str, actor_value: str, document) -> str:
        raise NotImplementedError()


class LocalActorResolutionAdapter(BaseActorResolutionAdapter):
    def resolve(self, actor_type: str, actor_value: str, document) -> str:
        if actor_type == "USER":
            return actor_value
        if actor_type == "ROLE":
            return f"role:{actor_value}"
        if actor_type == "POSITION":
            return f"position:{actor_value}"
        if actor_type == "DYNAMIC":
            if actor_value == "N+1_OF_ORIGINATOR" and document.originator:
                return f"dynamic:n+1:{document.originator_id}"
            return f"dynamic:{actor_value}"
        return actor_value


class CompassActorResolutionAdapter(LocalActorResolutionAdapter):
    def __init__(self):
        self.user_lookup_url = getattr(settings, "DOCGEN_COMPASS_DIRECTORY_USER_URL", "")
        self.role_lookup_url = getattr(settings, "DOCGEN_COMPASS_ORGCHART_ROLE_URL", "")
        self.position_lookup_url = getattr(settings, "DOCGEN_COMPASS_ORGCHART_POSITION_URL", "")
        self.manager_lookup_url = getattr(settings, "DOCGEN_COMPASS_ORGCHART_MANAGER_URL", "")
        self.timeout_seconds = float(getattr(settings, "DOCGEN_COMPASS_API_TIMEOUT_SECONDS", 3.0))
        self.api_token = getattr(settings, "DOCGEN_COMPASS_API_TOKEN", "")

    def resolve(self, actor_type: str, actor_value: str, document) -> str:
        if actor_type == "USER":
            payload = self._request_json(self.user_lookup_url, {"lookup": actor_value})
            extracted = self._extract_actor(payload)
            if extracted:
                return extracted
            return super().resolve(actor_type, actor_value, document)

        if actor_type == "ROLE":
            params = {"role": actor_value}
            if document.originator_id:
                params["originator_id"] = document.originator_id
            payload = self._request_json(self.role_lookup_url, params)
            extracted = self._extract_actor(payload)
            if extracted:
                return extracted
            return super().resolve(actor_type, actor_value, document)

        if actor_type == "POSITION":
            params = {"position": actor_value}
            payload = self._request_json(self.position_lookup_url, params)
            extracted = self._extract_actor(payload)
            if extracted:
                return extracted
            return super().resolve(actor_type, actor_value, document)

        if actor_type == "DYNAMIC" and actor_value == "N+1_OF_ORIGINATOR":
            if not document.originator_id:
                return super().resolve(actor_type, actor_value, document)
            payload = self._request_json(
                self.manager_lookup_url,
                {"originator_id": document.originator_id},
            )
            extracted = self._extract_actor(payload)
            if extracted:
                return extracted
            return super().resolve(actor_type, actor_value, document)

        return super().resolve(actor_type, actor_value, document)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request_json(self, base_url: str, params: dict) -> dict | list | None:
        if not base_url:
            return None

        query = urlencode(params)
        separator = "&" if "?" in base_url else "?"
        url = f"{base_url}{separator}{query}" if query else base_url

        request = Request(url, headers=self._headers())
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except Exception:
            return None

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _extract_actor(self, payload) -> str | None:
        if payload is None:
            return None

        if isinstance(payload, str) and payload:
            return payload

        if isinstance(payload, int):
            return str(payload)

        if isinstance(payload, list) and payload:
            return self._extract_actor(payload[0])

        if isinstance(payload, dict):
            for key in ["actor_value", "actor", "username", "user_id", "id", "value"]:
                value = payload.get(key)
                if value not in [None, ""]:
                    return str(value)

            nested_data = payload.get("data")
            if nested_data is not None:
                return self._extract_actor(nested_data)

        return None
