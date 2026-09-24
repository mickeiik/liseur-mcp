"""Async client for the liseur-sync native API (/v1)."""

from __future__ import annotations

from typing import Any

import httpx


class LiseurError(RuntimeError):
    """A non-2xx answer from liseur-sync."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("error") if isinstance(body, dict) else None
    detail = detail or response.text[:200].strip() or response.reason_phrase
    message = f"liseur-sync answered {response.status_code}: {detail}"
    if response.status_code == 403 and "scope" in str(detail).lower():
        message += " (the device token may be missing a required scope)"
    return message


class LiseurClient:
    """The endpoints this server reads, with cursor following where it applies."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        message = _error_message(response)
        if 300 <= response.status_code < 400:
            message += (
                " (redirects are not followed; LISEUR_URL may need to point at "
                "the redirected base URL)"
            )
        raise LiseurError(response.status_code, message)

    async def _request(
        self, method: str, path: str, *, max_bytes: int | None = None, **kwargs: Any
    ) -> Any:
        if max_bytes is None:
            response = await self._http.request(method, path, **kwargs)
            self._raise_for_status(response)
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return response.content
        async with self._http.stream(method, path, **kwargs) as response:
            if not response.is_success:
                await response.aread()
                self._raise_for_status(response)
            if response.headers.get("content-type", "").startswith("application/json"):
                await response.aread()
                return response.json()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise ValueError(
                        f"response from {path} is larger than the "
                        f"{max_bytes} byte cap; refusing to buffer it"
                    )
            return bytes(body)

    async def folders(self) -> list[dict[str, Any]]:
        folders: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if after:
                params["after"] = after
            page = await self._request("GET", "/v1/folders", params=params)
            folders.extend(page.get("folders", []))
            after = page.get("next_after")
            if not after:
                return folders

    async def books(
        self, folder_id: str, *, order: str = "recent", limit: int = 50
    ) -> list[dict[str, Any]]:
        page = await self._request(
            "GET", f"/v1/folders/{folder_id}/books", params={"order": order, "limit": limit}
        )
        return page.get("books", [])

    async def search(self, folder_id: str, query: str, limit: int) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/folders/{folder_id}/search", params={"q": query, "limit": limit}
        )

    async def book(self, book_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/books/{book_id}")

    async def resolve_book(self, book_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/books/{book_id}/resolve", json={})

    async def work_annotations(self, work_id: str) -> list[dict[str, Any]]:
        page = await self._request("GET", f"/v1/works/{work_id}/annotations")
        return page.get("annotations", [])

    async def annotation_changes(self) -> list[dict[str, Any]]:
        annotations: list[dict[str, Any]] = []
        since = 0
        while True:
            page = await self._request(
                "GET", "/v1/annotations/changes", params={"since": since, "limit": 500}
            )
            rows = page.get("annotations", [])
            annotations.extend(rows)
            next_since = rows[-1]["seq"] if rows else page.get("high_water", since)
            if next_since <= since:
                break
            since = next_since
            if not page.get("has_more"):
                break
        newest: dict[Any, dict[str, Any]] = {}
        for annotation in annotations:
            current = newest.get(annotation["id"])
            if current is None or annotation.get("rev", 0) >= current.get("rev", 0):
                newest[annotation["id"]] = annotation
        return [annotation for annotation in newest.values() if not annotation.get("deleted")]

    async def insights_summary(self, span: str) -> dict[str, Any]:
        return await self._request("GET", "/v1/insights/summary", params={"range": span})

    async def insights_works(self, span: str) -> dict[str, Any]:
        return await self._request("GET", "/v1/insights/works", params={"range": span})

    async def download(self, book_id: str, *, max_bytes: int) -> bytes:
        return await self._request(
            "GET", f"/v1/books/{book_id}/download", max_bytes=max_bytes
        )
