import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from demo_v2.domain.models import Asset, MarketInstance, MarketObservation, Timeframe, market_definitions
from demo_v2.streams import DecisionStream, ObservationRejected, TelemetryBuffer


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def observation(sequence=1, generation=1, price="0.74", **changes):
    return replace(MarketObservation(
        instance_id="instance-eth-hour", token_id="eth-up", generation=generation,
        sequence=sequence, observed_at=NOW, received_at=NOW, source="fake-public-feed",
        best_bid=Decimal("0.73"), best_ask=Decimal(price),
    ), **changes)


def test_generic_catalog_and_metadata_timing():
    definitions = market_definitions()
    assert len(set(definitions)) == 21
    assert {d.asset for d in definitions} == set(Asset)
    assert {d.timeframe.seconds for d in definitions} == {300, 900, 3600}
    definition = next(d for d in definitions if d.asset == Asset.HYPE and d.timeframe == Timeframe.H1)
    instance = MarketInstance("hour-no-slug-epoch", definition, "condition", "up", "down",
                              NOW, NOW + timedelta(hours=1))
    assert instance.ends_at - instance.starts_at == timedelta(hours=1)


def test_all_decision_prices_survive_telemetry_coalescing_and_sampling():
    async def run():
        stream = DecisionStream(capacity=8, clock=lambda: NOW)
        telemetry = TelemetryBuffer(capacity=1)
        stream.begin_generation(1)
        prices = ["0.75", "0.74", "0.73", "0.74"]
        for sequence, price in enumerate(prices, 1):
            item = observation(sequence, price=price)
            stream.publish(item)
            # The telemetry consumer intentionally samples and overwrites.
            if sequence % 2 == 0:
                telemetry.offer(item)
        assert [str((await stream.next_observation()).best_ask) for _ in prices] == prices
        assert [item.sequence for item in telemetry.drain()] == [4]
    asyncio.run(run())


@pytest.mark.parametrize("changes,reason", [
    ({"observed_at": NOW - timedelta(seconds=6)}, "FRESH"),
    ({"received_at": NOW - timedelta(seconds=6)}, "FRESH"),
    ({"observed_at": NOW + timedelta(seconds=1)}, "FRESH"),
    ({"book_ready": False}, "READY"),
    ({"generation": 2}, "GENERATION"),
    ({"sequence": 2}, "SEQUENCE"),
])
def test_invalid_observation_requires_new_generation(changes, reason):
    stream = DecisionStream(clock=lambda: NOW)
    stream.begin_generation(1)
    with pytest.raises(ObservationRejected, match=reason):
        stream.publish(observation(**changes))
    with pytest.raises(ObservationRejected):
        stream.publish(observation())
    stream.begin_generation(2)
    stream.publish(observation(generation=2))
    assert asyncio.run(stream.next_observation()).sequence == 1


def test_consumer_rechecks_age_after_queue_wait():
    clock = [NOW]
    stream = DecisionStream(clock=lambda: clock[0])
    stream.begin_generation(1)
    stream.publish(observation())
    clock[0] += timedelta(seconds=6)
    with pytest.raises(ObservationRejected, match="FRESH"):
        asyncio.run(stream.next_observation())


def test_overflow_discards_pending_decisions_instead_of_sampling():
    stream = DecisionStream(capacity=1, clock=lambda: NOW)
    stream.begin_generation(1)
    stream.publish(observation())
    with pytest.raises(ObservationRejected, match="OVERFLOW"):
        stream.publish(observation(2))
    with pytest.raises(ObservationRejected, match="OVERFLOW"):
        asyncio.run(stream.next_observation())
    stream.begin_generation(2)
    stream.publish(observation(generation=2))
    assert asyncio.run(stream.next_observation()).generation == 2


def test_reconnect_discards_old_queue_and_first_fresh_snapshot_can_be_delivered():
    stream = DecisionStream(clock=lambda: NOW)
    stream.begin_generation(1)
    stream.publish(observation())
    stream.invalidate("DISCONNECTED")
    stream.begin_generation(2)
    stream.publish(observation(generation=2))
    assert asyncio.run(stream.next_observation()).generation == 2
    with pytest.raises(ValueError, match="advance"):
        stream.begin_generation(2)


def test_generation_change_wakes_old_waiter_without_losing_first_new_observation():
    async def run():
        stream = DecisionStream(clock=lambda: NOW)
        stream.begin_generation(1)
        waiting = asyncio.create_task(stream.next_observation())
        await asyncio.sleep(0)
        stream.invalidate("DISCONNECTED")
        stream.begin_generation(2)
        stream.publish(observation(generation=2))
        with pytest.raises(ObservationRejected):
            await asyncio.wait_for(waiting, timeout=1)
        assert (await stream.next_observation()).generation == 2
    asyncio.run(run())


def test_unstarted_stream_and_repeated_sequence_fail_closed():
    stream = DecisionStream(clock=lambda: NOW)
    with pytest.raises(ObservationRejected, match="AWAITING"):
        stream.publish(observation())
    stream.begin_generation(1)
    stream.publish(observation())
    with pytest.raises(ObservationRejected, match="SEQUENCE"):
        stream.publish(observation())


@pytest.mark.parametrize("changes", [
    {"best_ask": Decimal("NaN")}, {"best_bid": Decimal("1.01")},
    {"best_ask": 0.74}, {"observed_at": NOW.replace(tzinfo=None)},
])
def test_observations_require_decimal_prices_and_aware_time(changes):
    with pytest.raises(ValueError):
        observation(**changes)
