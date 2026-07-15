import sqlite3
import sys
import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": headers})
        payload = self.payloads.pop(0) if self.payloads else []
        return FakeResponse(payload)


class CoinbaseVolumeTests(unittest.TestCase):
    def test_coinbase_candle_params_use_current_bucket_and_now(self):
        now_dt = datetime(2026, 7, 16, 12, 3, 45, 123456, tzinfo=timezone.utc)

        params = app.coinbase_candle_params(now_dt)

        self.assertEqual(params["granularity"], 300)
        self.assertEqual(params["start"], "2026-07-16T12:00:00Z")
        self.assertEqual(params["end"], "2026-07-16T12:03:45Z")

    def test_select_current_candle_by_timestamp_not_index(self):
        now_dt = datetime.fromtimestamp(1_800_000_150, timezone.utc)
        bucket = app.floor_to_epoch(now_dt, app.COINBASE_CANDLE_GRANULARITY_SECONDS)
        candles = [
            [bucket - 300, 1, 2, 1, 2, 10.0],
            [bucket + 300, 1, 2, 1, 2, 12.0],
            [bucket, 1, 2, 1, 2, 11.5],
        ]

        candle, error = app.select_current_coinbase_candle(candles, now_dt)

        self.assertIsNone(error)
        self.assertEqual(candle["candle_start_epoch"], bucket)
        self.assertEqual(candle["volume_btc_cumulative"], 11.5)

    def test_select_current_candle_rejects_missing_current_bucket(self):
        now_dt = datetime.fromtimestamp(1_800_000_150, timezone.utc)
        bucket = app.floor_to_epoch(now_dt, app.COINBASE_CANDLE_GRANULARITY_SECONDS)

        candle, error = app.select_current_coinbase_candle(
            [[bucket - 300, 1, 2, 1, 2, 10.0]],
            now_dt,
        )

        self.assertIsNone(candle)
        self.assertEqual(error, "Current Coinbase candle not found")

    def test_delta_baseline_without_previous_sample(self):
        sampled_at = datetime(2026, 7, 16, 12, 0, 30, tzinfo=timezone.utc)

        delta, seconds, status, error = app.calculate_coinbase_delta(
            None,
            "2026-07-16T12:00:00+00:00",
            sampled_at,
            5.0,
        )

        self.assertIsNone(delta)
        self.assertIsNone(seconds)
        self.assertEqual(status, "baseline")
        self.assertEqual(error, "no previous valid sample")

    def test_delta_success_same_candle(self):
        sampled_at = datetime(2026, 7, 16, 12, 0, 30, tzinfo=timezone.utc)
        previous = {
            "sampled_at": (sampled_at - timedelta(seconds=30)).isoformat(),
            "candle_start_at": "2026-07-16T12:00:00+00:00",
            "volume_btc_cumulative": 4.25,
        }

        delta, seconds, status, error = app.calculate_coinbase_delta(
            previous,
            "2026-07-16T12:00:00+00:00",
            sampled_at,
            5.0,
        )

        self.assertEqual(delta, 0.75)
        self.assertEqual(seconds, 30)
        self.assertEqual(status, "success")
        self.assertIsNone(error)

    def test_delta_baseline_for_new_candle_negative_delta_and_long_gap(self):
        sampled_at = datetime(2026, 7, 16, 12, 5, 30, tzinfo=timezone.utc)
        previous = {
            "sampled_at": (sampled_at - timedelta(seconds=30)).isoformat(),
            "candle_start_at": "2026-07-16T12:00:00+00:00",
            "volume_btc_cumulative": 8.0,
        }

        delta, _, status, error = app.calculate_coinbase_delta(
            previous,
            "2026-07-16T12:05:00+00:00",
            sampled_at,
            1.0,
        )
        self.assertIsNone(delta)
        self.assertEqual(status, "baseline")
        self.assertEqual(error, "new candle")

        previous_same_candle = {
            "sampled_at": (sampled_at - timedelta(seconds=30)).isoformat(),
            "candle_start_at": "2026-07-16T12:05:00+00:00",
            "volume_btc_cumulative": 8.0,
        }
        delta, _, status, error = app.calculate_coinbase_delta(
            previous_same_candle,
            "2026-07-16T12:05:00+00:00",
            sampled_at,
            7.0,
        )
        self.assertIsNone(delta)
        self.assertEqual(status, "baseline")
        self.assertEqual(error, "cumulative volume decreased within same candle")

        previous_old = {
            "sampled_at": (sampled_at - timedelta(seconds=120)).isoformat(),
            "candle_start_at": "2026-07-16T12:05:00+00:00",
            "volume_btc_cumulative": 2.0,
        }
        delta, seconds, status, error = app.calculate_coinbase_delta(
            previous_old,
            "2026-07-16T12:05:00+00:00",
            sampled_at,
            3.0,
        )
        self.assertIsNone(delta)
        self.assertEqual(seconds, 120)
        self.assertEqual(status, "baseline")
        self.assertEqual(error, "gap exceeded max delta threshold")

    def test_init_db_and_unique_bucket_are_idempotent(self):
        original_db_path = app.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.DB_PATH = Path(temp_dir) / "test.sqlite3"
                app.init_db()
                app.init_db()

                row = {
                    "sampled_at": "2026-07-16T12:00:30+00:00",
                    "sample_bucket_at": "2026-07-16T12:00:30+00:00",
                    "candle_start_at": "2026-07-16T12:00:00+00:00",
                    "product_id": "BTC-USD",
                    "granularity_seconds": 300,
                    "volume_btc_cumulative": 1.0,
                    "volume_btc_delta": None,
                    "seconds_since_previous_sample": None,
                    "event_slug": "event",
                    "condition_id": "condition",
                    "source": "coinbase_exchange",
                    "status": "baseline",
                    "error": None,
                }

                first_insert = app.insert_btc_volume_log(row)
                second_insert = app.insert_btc_volume_log(row)

                conn = sqlite3.connect(app.DB_PATH)
                try:
                    count = conn.execute("SELECT COUNT(*) FROM btc_volume_log").fetchone()[0]
                    indexes = [
                        item[1]
                        for item in conn.execute("PRAGMA index_list(btc_volume_log)").fetchall()
                    ]
                finally:
                    conn.close()

                self.assertTrue(first_insert)
                self.assertFalse(second_insert)
                self.assertEqual(count, 1)
                self.assertIn("idx_btc_volume_log_unique_bucket", indexes)
        finally:
            app.DB_PATH = original_db_path

    def test_fetch_current_coinbase_candle_retry_succeeds_second_attempt(self):
        original_retry_count = app.COINBASE_MISSING_CANDLE_RETRY_COUNT
        original_retry_delay = app.COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS
        original_now_utc = app.now_utc
        try:
            app.COINBASE_MISSING_CANDLE_RETRY_COUNT = 1
            app.COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS = 0
            now_dt = datetime(2026, 7, 16, 12, 3, 0, tzinfo=timezone.utc)
            app.now_utc = lambda: now_dt
            bucket = app.floor_to_epoch(now_dt, app.COINBASE_CANDLE_GRANULARITY_SECONDS)
            client = FakeAsyncClient([
                [[bucket - 300, 1, 2, 1, 2, 1.0]],
                [[bucket, 1, 2, 1, 2, 2.5]],
            ])

            candle, _, error, attempts, _ = asyncio.run(app.fetch_current_coinbase_candle(client))

            self.assertIsNone(error)
            self.assertEqual(candle["candle_start_epoch"], bucket)
            self.assertEqual(candle["volume_btc_cumulative"], 2.5)
            self.assertEqual(attempts, 2)
            self.assertEqual(len(client.calls), 2)
            self.assertIn("start", client.calls[0]["params"])
            self.assertIn("end", client.calls[0]["params"])
        finally:
            app.COINBASE_MISSING_CANDLE_RETRY_COUNT = original_retry_count
            app.COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS = original_retry_delay
            app.now_utc = original_now_utc

    def test_fetch_current_coinbase_candle_retry_fails_all_attempts(self):
        original_retry_count = app.COINBASE_MISSING_CANDLE_RETRY_COUNT
        original_retry_delay = app.COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS
        original_now_utc = app.now_utc
        try:
            app.COINBASE_MISSING_CANDLE_RETRY_COUNT = 2
            app.COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS = 0
            now_dt = datetime(2026, 7, 16, 12, 3, 0, tzinfo=timezone.utc)
            app.now_utc = lambda: now_dt
            bucket = app.floor_to_epoch(now_dt, app.COINBASE_CANDLE_GRANULARITY_SECONDS)
            client = FakeAsyncClient([
                [[bucket - 300, 1, 2, 1, 2, 1.0]],
                [[bucket - 300, 1, 2, 1, 2, 1.0]],
                [[bucket - 300, 1, 2, 1, 2, 1.0]],
            ])

            candle, _, error, attempts, _ = asyncio.run(app.fetch_current_coinbase_candle(client))

            self.assertIsNone(candle)
            self.assertEqual(error, "Current Coinbase candle not found")
            self.assertEqual(attempts, 3)
            self.assertEqual(len(client.calls), 3)
        finally:
            app.COINBASE_MISSING_CANDLE_RETRY_COUNT = original_retry_count
            app.COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS = original_retry_delay
            app.now_utc = original_now_utc


if __name__ == "__main__":
    unittest.main()
