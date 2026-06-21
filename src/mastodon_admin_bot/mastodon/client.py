from __future__ import annotations

from typing import Any

import httpx


class MastodonApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class MastodonClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client

    async def __aenter__(self) -> MastodonClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=20)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MastodonClient must be used as an async context manager")
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def exchange_oauth_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        response = await self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        return self._json_or_error(response)

    async def verify_credentials(self) -> dict[str, Any]:
        response = await self.client.get(
            "/api/v1/accounts/verify_credentials",
            headers=self._headers(),
        )
        return self._json_or_error(response)

    async def approve_account(self, account_id: str) -> dict[str, Any]:
        response = await self.client.post(
            f"/api/v1/admin/accounts/{account_id}/approve",
            headers=self._headers(),
        )
        return self._json_or_error(response)

    async def reject_account(self, account_id: str) -> dict[str, Any]:
        response = await self.client.post(
            f"/api/v1/admin/accounts/{account_id}/reject",
            headers=self._headers(),
        )
        return self._json_or_error(response)

    async def resolve_report(self, report_id: str) -> dict[str, Any]:
        response = await self.client.post(
            f"/api/v1/admin/reports/{report_id}/resolve",
            headers=self._headers(),
        )
        return self._json_or_error(response)

    async def account_action(
        self,
        *,
        account_id: str,
        action_type: str,
        report_id: str | None = None,
        text: str | None = None,
        send_email_notification: bool = True,
    ) -> dict[str, Any]:
        data: dict[str, str | bool] = {
            "type": action_type,
            "send_email_notification": send_email_notification,
        }
        if report_id:
            data["report_id"] = report_id
        if text:
            data["text"] = text
        response = await self.client.post(
            f"/api/v1/admin/accounts/{account_id}/action",
            headers=self._headers(),
            data=data,
        )
        return self._json_or_error(response)

    def _json_or_error(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_success:
            data = response.json()
            if isinstance(data, dict):
                return data
            raise MastodonApiError(response.status_code, "unexpected non-object response")
        raise self._api_error(response)

    def _empty_or_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        raise self._api_error(response)

    def _api_error(self, response: httpx.Response) -> MastodonApiError:
        message = response.text
        try:
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                message = str(data["error"])
        except ValueError:
            pass
        return MastodonApiError(response.status_code, message)
