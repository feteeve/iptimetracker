"""Optional, read-only diagnostics for routers with the Flutter JSON API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class IptimeDiagnosticsClient:
    """Use an independent session so JSON API failures cannot break tracking."""

    def __init__(self, host: str, username: str, password: str) -> None:
        self._url = f"http://{host}/cgi/service.cgi"
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._cookie: str | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _request(self, method: str, params: dict | None = None) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Cookie": f"efm_session_id={self._cookie}"} if self._cookie else {}
        async with self._session.post(
            self._url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=4),
        ) as response:
            if response.status != 200:
                raise ValueError(f"JSON API HTTP {response.status}")
            result = await response.json(content_type=None)
            if not isinstance(result, dict):
                raise ValueError("Invalid JSON API response")
            if method == "session/login" and result.get("result") == "done":
                cookie = response.cookies.get("efm_session_id")
                if cookie:
                    self._cookie = cookie.value
            return result

    async def _login(self) -> bool:
        self._cookie = None
        response = await self._request(
            "session/login", {"id": self._username, "pw": self._password}
        )
        return response.get("result") == "done"

    async def _read(self, method: str) -> Any:
        response = await self._request(method)
        if response.get("error") is not None or "result" not in response:
            return None
        return response["result"]

    async def fetch(self) -> dict[str, Any]:
        """Return available diagnostics; unsupported methods stay absent."""
        try:
            if not self._cookie and not await self._login():
                return {}
            methods = {
                "system": "system/info",
                "wan": "network/interface/wan1/info",
                "wan_config": "network/interface/wan1/config",
                "nat": "nat/config",
                "port_role": "port/role",
                "dns": "network/dns/info",
                "firmware": "firmware/info",
                "ports": "port/link/status",
                "mesh": "easymesh/info",
                "mesh_agents": "easymesh/show/agent",
            }
            async def read_one(key: str, method: str) -> tuple[str, Any]:
                try:
                    return key, await self._read(method)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    _LOGGER.debug("Optional ipTIME diagnostic unavailable: %s", method)
                    return key, None

            data = {
                key: value
                for key, value in await asyncio.gather(
                    *(read_one(key, method) for key, method in methods.items())
                )
                if value is not None
            }
            if not data:
                self._cookie = None
            return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            self._cookie = None
            _LOGGER.debug("ipTIME JSON diagnostics unavailable; legacy tracking continues")
            return {}
