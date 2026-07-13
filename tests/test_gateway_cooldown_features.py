import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import main


class GatewayCooldownFeatureTests(unittest.TestCase):
    def setUp(self):
        main._cooldown_reasons.clear()
        with main._channel_health_lock:
            main._channel_health_buckets.clear()

    def _isolated_paths(self, root: Path):
        data_dir = root / "data"
        return mock.patch.multiple(
            main,
            DATA_DIR=data_dir,
            DB_PATH=data_dir / "gateway.db",
            CONFIG_PATH=data_dir / "config.json",
        )

    def _insert_channel(
        self,
        channel_id: str,
        *,
        enabled: bool,
        downstream_api_key: str | None = None,
        cooldown_until: float | None = None,
    ) -> None:
        with main._connect_db() as conn:
            now = main._utc_now()
            conn.execute(
                """
                INSERT INTO channels(
                    id, name, upstream_url, priority, enabled,
                    upstream_api_key, downstream_api_key, inject_wait_tool,
                    supported_models, proxy_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    channel_id,
                    "https://example.com/v1",
                    0,
                    1 if enabled else 0,
                    "upstream-key",
                    downstream_api_key,
                    0,
                    json.dumps(["gpt-5.5"]),
                    None,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO channel_runtime(channel_id, consecutive_failures, cooldown_until, updated_at)
                VALUES (?, 0, ?, ?)
                """,
                (channel_id, cooldown_until, main._now_ts()),
            )
            conn.commit()

    def _request(self, token: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"},
            app=types.SimpleNamespace(state=types.SimpleNamespace(client_api_key="global-key")),
        )

    def test_cooldown_config_defaults_and_validates_range(self):
        with tempfile.TemporaryDirectory() as raw:
            with self._isolated_paths(Path(raw)):
                self.assertEqual(main._get_cooldown_minutes(), 5)
                main._write_app_config({"cooldown_minutes": 30})
                self.assertEqual(main._get_cooldown_seconds(), 1800)
                self.assertEqual(main._validate_cooldown_minutes(5), 5)
                self.assertEqual(main._validate_cooldown_minutes(180), 180)
                with self.assertRaises(main.GatewayError):
                    main._validate_cooldown_minutes(4)
                with self.assertRaises(main.GatewayError):
                    main._validate_cooldown_minutes(181)

    def test_global_key_filters_disabled_and_cooling_channels(self):
        with tempfile.TemporaryDirectory() as raw:
            with self._isolated_paths(Path(raw)):
                main._init_db()
                with main._connect_db() as conn:
                    conn.execute("DELETE FROM channels")
                    conn.commit()
                self._insert_channel("disabled", enabled=False)
                self._insert_channel("cooling", enabled=True, cooldown_until=main._now_ts() + 300)

                channels, is_global_key = main._select_channels(self._request("global-key"), "gpt-5.5")

                self.assertTrue(is_global_key)
                self.assertEqual(channels, [])

    def test_dedicated_key_ignores_enabled_and_cooldown(self):
        with tempfile.TemporaryDirectory() as raw:
            with self._isolated_paths(Path(raw)):
                main._init_db()
                with main._connect_db() as conn:
                    conn.execute("DELETE FROM channels")
                    conn.commit()
                self._insert_channel(
                    "dedicated",
                    enabled=False,
                    downstream_api_key="dedicated-key",
                    cooldown_until=main._now_ts() + 300,
                )

                channels, is_global_key = main._select_channels(self._request("dedicated-key"), "gpt-5.5")

                self.assertFalse(is_global_key)
                self.assertEqual([channel["id"] for channel in channels], ["dedicated"])

    def test_cooldown_reason_is_extracted_and_kept_in_memory(self):
        with tempfile.TemporaryDirectory() as raw:
            with self._isolated_paths(Path(raw)):
                main._init_db()
                with main._connect_db() as conn:
                    conn.execute("DELETE FROM channels")
                    conn.commit()
                self._insert_channel("reason-channel", enabled=True)
                main._cooldown_reasons.clear()
                reason = main._upstream_error_reason_from_bytes(
                    429,
                    b'{"error":{"message":"upstream rate limit"}}',
                )

                for _ in range(3):
                    main._record_channel_result(
                        "reason-channel",
                        False,
                        429,
                        "upstream_error",
                        10,
                        True,
                        True,
                        reason,
                    )

                self.assertEqual(main._cooldown_reasons["reason-channel"], "upstream rate limit")
                with main._connect_db() as conn:
                    public = main._public_channel(main._get_channel_row(conn, "reason-channel"), conn)
                self.assertEqual(public["cooldown_reason"], "upstream rate limit")

                main._record_channel_result(
                    "reason-channel",
                    True,
                    200,
                    None,
                    10,
                    False,
                    True,
                )
                self.assertNotIn("reason-channel", main._cooldown_reasons)

    def test_channel_health_snapshot_returns_recent_15_buckets(self):
        current = 100 * main.HEALTH_BUCKET_SECONDS + 42
        current_start = main._health_bucket_start(current)
        oldest_start = current_start - (main.HEALTH_BUCKET_COUNT - 1) * main.HEALTH_BUCKET_SECONDS

        main._record_channel_health("health-channel", True, oldest_start + 1)
        main._record_channel_health("health-channel", False, oldest_start + main.HEALTH_BUCKET_SECONDS + 1)
        main._record_channel_health("health-channel", True, oldest_start + 2 * main.HEALTH_BUCKET_SECONDS + 1)
        main._record_channel_health("health-channel", False, oldest_start + 2 * main.HEALTH_BUCKET_SECONDS + 2)

        snapshot = main._channel_health_snapshot("health-channel", current)

        self.assertEqual(len(snapshot), main.HEALTH_BUCKET_COUNT)
        self.assertEqual(snapshot[0]["state"], "success")
        self.assertEqual(snapshot[0]["success"], 1)
        self.assertEqual(snapshot[0]["failure"], 0)
        self.assertEqual(snapshot[1]["state"], "failure")
        self.assertEqual(snapshot[1]["success"], 0)
        self.assertEqual(snapshot[1]["failure"], 1)
        self.assertEqual(snapshot[2]["state"], "mixed")
        self.assertEqual(snapshot[2]["success"], 1)
        self.assertEqual(snapshot[2]["failure"], 1)
        self.assertTrue(all(bucket["state"] == "empty" for bucket in snapshot[3:]))

    def test_channel_health_uses_half_open_5_minute_buckets(self):
        current = 200 * main.HEALTH_BUCKET_SECONDS + 10
        current_start = main._health_bucket_start(current)
        previous_start = current_start - main.HEALTH_BUCKET_SECONDS

        main._record_channel_health("boundary-channel", True, current_start - 0.001)
        main._record_channel_health("boundary-channel", False, current_start)

        snapshot = main._channel_health_snapshot("boundary-channel", current)
        by_start = {bucket["start_at"]: bucket for bucket in snapshot}

        previous_bucket = by_start[main._ts_to_iso(float(previous_start))]
        current_bucket = by_start[main._ts_to_iso(float(current_start))]
        self.assertEqual(previous_bucket["state"], "success")
        self.assertEqual(current_bucket["state"], "failure")

    def test_public_channel_includes_health_snapshot_and_clear_removes_it(self):
        with tempfile.TemporaryDirectory() as raw:
            with self._isolated_paths(Path(raw)):
                main._init_db()
                with main._connect_db() as conn:
                    conn.execute("DELETE FROM channels")
                    conn.commit()
                self._insert_channel("public-health", enabled=True)

                fixed_now = 300 * main.HEALTH_BUCKET_SECONDS + 10
                with mock.patch.object(main, "_now_ts", return_value=fixed_now):
                    main._record_channel_health("public-health", True)

                    with main._connect_db() as conn:
                        public = main._public_channel(main._get_channel_row(conn, "public-health"), conn)

                self.assertEqual(len(public["health_5m"]), main.HEALTH_BUCKET_COUNT)
                self.assertEqual(public["health_5m"][-1]["state"], "success")

                main._clear_channel_health("public-health")
                self.assertTrue(all(bucket["state"] == "empty" for bucket in main._channel_health_snapshot("public-health")))


if __name__ == "__main__":
    unittest.main()
