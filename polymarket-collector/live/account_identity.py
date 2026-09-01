from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from .repository import now_iso


ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass
class AccountIdentityResult:
    status: str
    configured_profile_address: str
    resolved_proxy_wallet: str | None = None
    expected_funder_candidate: str | None = None
    public_positions_count: int = 0
    public_positions_value: float | None = None
    public_closed_positions_count: int = 0
    public_activity_count: int = 0
    refreshed_at: str | None = None
    error: str = ""
    raw_public_payload: dict[str, Any] | None = None


class PublicAccountIdentityClient:
    def __init__(self, data_host: str = "https://data-api.polymarket.com", timeout: float = 10):
        self.data_host = data_host.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def validate_address(address: str) -> bool:
        return bool(ADDRESS_RE.match(address or ""))

    async def resolve(self, profile_address: str) -> AccountIdentityResult:
        if not self.validate_address(profile_address):
            return AccountIdentityResult("INVALID_ADDRESS", profile_address, error="Profile address must be a 0x-prefixed Ethereum address")
        params = {"user": profile_address, "limit": 500, "sizeThreshold": 0}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                positions_response = await client.get(f"{self.data_host}/positions", params=params)
                positions_response.raise_for_status()
                positions = positions_response.json()
                closed_response = await client.get(f"{self.data_host}/positions", params={**params, "redeemable": "true"})
                closed_positions = closed_response.json() if closed_response.status_code < 400 else []
                trades_response = await client.get(f"{self.data_host}/trades", params={"user": profile_address, "limit": 100})
                trades = trades_response.json() if trades_response.status_code < 400 else []
        except httpx.TimeoutException:
            return AccountIdentityResult("UNAVAILABLE", profile_address, error="Public account request timed out")
        except httpx.HTTPStatusError as exc:
            status = "PROFILE_NOT_FOUND" if exc.response.status_code == 404 else "UNAVAILABLE"
            return AccountIdentityResult(status, profile_address, error=f"Public account request failed: HTTP {exc.response.status_code}")
        except Exception as exc:
            return AccountIdentityResult("UNAVAILABLE", profile_address, error=f"{type(exc).__name__}: {exc}")

        if not isinstance(positions, list):
            return AccountIdentityResult("UNEXPECTED_RESPONSE", profile_address, error="Positions response was not a list")
        proxy_wallet = None
        total_value = 0.0
        for position in positions:
            if isinstance(position, dict):
                proxy_wallet = proxy_wallet or position.get("proxyWallet")
                try:
                    total_value += float(position.get("currentValue") or 0)
                except (TypeError, ValueError):
                    pass
        status = "UNVERIFIED"
        if proxy_wallet and str(proxy_wallet).lower() != profile_address.lower():
            status = "UNVERIFIED"
        return AccountIdentityResult(
            status=status,
            configured_profile_address=profile_address,
            resolved_proxy_wallet=proxy_wallet,
            expected_funder_candidate=proxy_wallet,
            public_positions_count=len(positions),
            public_positions_value=total_value,
            public_closed_positions_count=len(closed_positions) if isinstance(closed_positions, list) else 0,
            public_activity_count=len(trades) if isinstance(trades, list) else 0,
            refreshed_at=now_iso(),
            raw_public_payload={"positions_sample": positions[:5], "trades_sample": trades[:5] if isinstance(trades, list) else []},
        )

    async def redemption_activity(
        self, profile_address: str, condition_id: str
    ) -> list[dict[str, Any]]:
        """Return public on-chain redemption evidence for one account/market."""
        if not self.validate_address(profile_address):
            raise ValueError("invalid profile address")
        if not re.match(r"^0x[a-fA-F0-9]{64}$", condition_id or ""):
            raise ValueError("invalid condition id")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.data_host}/activity",
                    params={
                        "user": profile_address,
                        "market": condition_id,
                        "type": "REDEEM",
                        "limit": 100,
                        "sortBy": "TIMESTAMP",
                        "sortDirection": "DESC",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as exc:
            raise RuntimeError("public redemption request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"public redemption request failed: HTTP {exc.response.status_code}"
            ) from exc
        if not isinstance(payload, list):
            raise RuntimeError("public redemption response was not a list")
        return [dict(item) for item in payload if isinstance(item, dict)]


class MockPublicAccountIdentityClient(PublicAccountIdentityClient):
    async def resolve(self, profile_address: str) -> AccountIdentityResult:
        if not self.validate_address(profile_address):
            return AccountIdentityResult("INVALID_ADDRESS", profile_address, error="invalid mock address")
        return AccountIdentityResult(
            status="UNVERIFIED",
            configured_profile_address=profile_address,
            resolved_proxy_wallet=profile_address,
            expected_funder_candidate=profile_address,
            public_positions_count=1,
            public_positions_value=1.23,
            public_closed_positions_count=0,
            public_activity_count=2,
            refreshed_at=now_iso(),
            raw_public_payload={"mock": True},
        )
