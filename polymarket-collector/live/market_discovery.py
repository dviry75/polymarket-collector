from __future__ import annotations
import json
from datetime import datetime, timezone
import httpx
from .repository import LiveRepository, now_iso

GAMMA_EVENT = "https://gamma-api.polymarket.com/events/slug/{slug}"

async def refresh_btc_5m_markets(repo: LiveRepository) -> list[str]:
    base = (int(datetime.now(timezone.utc).timestamp()) // 300) * 300
    slugs = [f"btc-updown-5m-{base}", f"btc-updown-5m-{base + 300}"]
    found = []
    async with httpx.AsyncClient(timeout=10) as client:
        for slug in slugs:
            try:
                response = await client.get(GAMMA_EVENT.format(slug=slug))
                if response.status_code != 200:
                    continue
                event = response.json()
                markets = event.get("markets") or []
                if not markets:
                    continue
                market = markets[0]
                outcomes = market.get("outcomes") or []
                tokens = market.get("clobTokenIds") or []
                if isinstance(outcomes, str): outcomes = json.loads(outcomes)
                if isinstance(tokens, str): tokens = json.loads(tokens)
                mapping = {str(o).lower(): str(t) for o, t in zip(outcomes, tokens)}
                condition = str(market.get("conditionId") or "")
                if not condition:
                    continue
                repo.upsert_market({
                    "event_id": slug, "condition_id": condition,
                    "yes_token_id": mapping.get("up") or mapping.get("yes"),
                    "no_token_id": mapping.get("down") or mapping.get("no"),
                    "token_mapping_status": "verified" if len(mapping) >= 2 else "unverified",
                    "accepting_orders": market.get("acceptingOrders") is True,
                    "market_resolved": market.get("closed") is True,
                    "source": "gamma_public_read_only", "last_update_at": now_iso(),
                    "raw_market_info": {"slug": slug, "question": market.get("question"),
                                        "startDate": market.get("startDate"), "endDate": market.get("endDate")},
                })
                found.append(condition)
            except Exception:
                continue
    return found
