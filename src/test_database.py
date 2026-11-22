#!/usr/bin/python3
# SPDX-License-Identifier: MIT

"""
This module contains unit tests for the datbase functions in the amd-debug-tools package.
"""

import os
import unittest

from datetime import datetime
from unittest.mock import patch, MagicMock

from amd_debug.database import SCHEMA_VERSION, SleepDatabase, default_database_path


class TestTriageMigration(unittest.TestCase):
    """Test migrating a schema 4 database to add triage tables"""

    def test_migrate_to_triage(self):
        """Test the triage tables are created from schema 4"""
        import sqlite3  # pylint: disable=import-outside-toplevel
        from amd_debug.database import (
            migrate,
        )  # pylint: disable=import-outside-toplevel

        cur = sqlite3.connect(":memory:").cursor()
        cur.execute("PRAGMA user_version = 4")
        migrate(cur, SCHEMA_VERSION)
        cur.execute("SELECT name FROM sqlite_master WHERE name LIKE 'triage%'")
        self.assertEqual(
            sorted(r[0] for r in cur.fetchall()), ["triage", "triage_event"]
        )
        cur.execute("PRAGMA user_version")
        self.assertEqual(cur.fetchone()[0], SCHEMA_VERSION)

    def test_migrate_verified_backfill(self):
        """Test the verified column is backfilled from the prose messages"""
        import sqlite3  # pylint: disable=import-outside-toplevel
        from amd_debug.database import (
            migrate,
        )  # pylint: disable=import-outside-toplevel

        cur = sqlite3.connect(":memory:").cursor()
        cur.execute("PRAGMA user_version = 5")
        cur.execute(
            "CREATE TABLE triage_event (session INTEGER, id INTEGER, ts INTEGER, "
            "kind TEXT, message TEXT, answer TEXT, PRIMARY KEY(session, id))"
        )
        cur.execute(
            "INSERT INTO triage_event (session, id, ts, kind, message, answer) "
            "VALUES (1, 0, 0, 'action', 'Change applied: IOMMU disabled', NULL)"
        )
        cur.execute(
            "INSERT INTO triage_event (session, id, ts, kind, message, answer) "
            "VALUES (1, 1, 0, 'action', "
            "'Change applied (unverified): IOMMU disabled', NULL)"
        )
        # A non-action event with a similar message must not be backfilled
        cur.execute(
            "INSERT INTO triage_event (session, id, ts, kind, message, answer) "
            "VALUES (1, 2, 0, 'info', 'Change applied: IOMMU disabled', NULL)"
        )
        migrate(cur, SCHEMA_VERSION)
        cur.execute("SELECT verified FROM triage_event ORDER BY id")
        self.assertEqual([r[0] for r in cur.fetchall()], [1, 0, None])

    def test_migrate_schema_0_idempotent_priority(self):
        """Test migrating from schema 0 when debug already has priority column"""
        import sqlite3  # pylint: disable=import-outside-toplevel
        from amd_debug.database import (
            migrate,
        )  # pylint: disable=import-outside-toplevel

        cur = sqlite3.connect(":memory:").cursor()
        cur.execute(
            "CREATE TABLE debug (t0 INTEGER, id INTEGER, message TEXT, priority INTEGER)"
        )
        cur.execute("PRAGMA user_version = 0")
        migrate(cur, SCHEMA_VERSION)
        cur.execute("PRAGMA user_version")
        self.assertEqual(cur.fetchone()[0], SCHEMA_VERSION)

    def test_new_database_file_init(self):
        """Test initializing a new SleepDatabase on a filesystem path"""
        import tempfile  # pylint: disable=import-outside-toplevel
        from amd_debug.database import (
            SleepDatabase,
        )  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = SleepDatabase(db_path)
            cur = db.db.cursor()
            cur.execute("PRAGMA user_version")
            self.assertEqual(cur.fetchone()[0], SCHEMA_VERSION)


class TestSleepDatabase(unittest.TestCase):
    """Test SleepDatabase class"""

    def setUp(self):
        """Set up mocks and an in-memory database for testing"""
        # Initialize SleepDatabase after mocks are set up
        self.db = SleepDatabase(dbf=":memory:")

    def test_start_cycle(self):
        """Test starting a new sleep cycle"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.assertEqual(self.db.last_suspend, timestamp)
        self.assertEqual(self.db.cycle_data_cnt, 0)
        self.assertEqual(self.db.debug_cnt, 0)

    def test_record_debug(self):
        """Test recording a debug message"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_debug("Test debug message", level=5)
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT message, priority FROM debug WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, ("Test debug message", 5))

    def test_record_battery_energy(self):
        """Test recording battery energy"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_battery_energy("Battery1", 50, 100, "mWh")
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT name, b0, b1, full, unit FROM battery WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, ("Battery1", 50, None, 100, "mWh"))

    def test_record_cycle_data(self):
        """Test recording cycle data"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle_data("Test cycle data", "symbol1")
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT message, symbol FROM cycle_data WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, ("Test cycle data", "symbol1"))

    def test_s0ix_baseline(self):
        """Test storing and fetching the S0ix residency baseline"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_s0ix_baseline(1000000, {"S0i3.0": 240})
        baseline = self.db.get_s0ix_baseline()
        self.assertEqual(baseline, (1000000, {"S0i3.0": 240}))

    def test_s0ix_baseline_missing(self):
        """Test that fetching a missing baseline returns None"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.assertIsNone(self.db.get_s0ix_baseline())

    def test_s0ix_baseline_replaced(self):
        """Test that a baseline is replaced for the same cycle"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_s0ix_baseline(1000000, {})
        self.db.record_s0ix_baseline(2000000, {"S0i2.0": 5})
        baseline = self.db.get_s0ix_baseline()
        self.assertEqual(baseline, (2000000, {"S0i2.0": 5}))

    def test_pci_power_state(self):
        """Test storing and fetching the PCI device power states"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        states = {
            "0000:2b:00.0": ("iwlwifi", "suspended", "1"),
            "0000:00:14.3": ("", "active", "1"),
        }
        self.db.record_pci_power_state(states)
        result = self.db.get_pci_power_state()
        self.assertEqual(result, states)

    def test_pci_power_state_missing(self):
        """Test that fetching missing PCI power states returns empty"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.assertEqual(self.db.get_pci_power_state(), {})

    def test_migration_from_v0(self):
        """Test migration of a version 0 database to the current schema"""
        import sqlite3 as sqlite3_mod
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            dbf = tmp.name
        con = sqlite3_mod.connect(dbf)
        con.execute(
            "CREATE TABLE debug (t0 INTEGER, id INTEGER, message TEXT, "
            "PRIMARY KEY(t0, id))"
        )
        con.execute("PRAGMA user_version = 0")
        con.commit()
        con.close()

        db = SleepDatabase(dbf=dbf)
        cur = db.db.cursor()
        columns = [row[1] for row in cur.execute("PRAGMA table_info(debug)").fetchall()]
        self.assertIn("priority", columns)
        cur.execute("PRAGMA user_version")
        self.assertEqual(cur.fetchone()[0], SCHEMA_VERSION)
        db.db.close()
        os.remove(dbf)

    def test_record_debug_file_permission_error(self):
        """Test that an unreadable file is recorded as a notice"""
        self.db.start_cycle(datetime.now())
        with patch("amd_debug.database.read_file", side_effect=PermissionError):
            self.db.record_debug_file("/sys/protected")
        cur = self.db.db.cursor()
        result = cur.execute(
            "SELECT message FROM debug WHERE message LIKE 'Unable to capture%'"
        ).fetchall()
        self.assertEqual(len(result), 1)

    def test_report_battery_and_power_rails(self):
        """Test the battery and power rail report helpers"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_battery_energy("BAT0", 1000, 2000, "uWh")
        self.db.record_power_rail_energy("rail", 10, 0.5)
        self.assertEqual(len(self.db.report_battery()), 1)
        self.assertEqual(len(self.db.report_battery(timestamp)), 1)
        self.assertEqual(
            self.db.report_power_rails(),
            [(int(timestamp.strftime("%Y%m%d%H%M%S")), "rail", 10.0, None, 0.5)],
        )

    def test_report_battery_no_cycle(self):
        """Test the report helpers without a started cycle"""
        with self.assertRaises(AssertionError):
            self.db.report_battery()

    def test_report_summary_dataframe(self):
        """Test the summary dataframe report"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle(
            requested_duration=10,
            active_gpios="",
            wakeup_irqs="9",
            kernel_duration=1.0,
            hw_sleep_duration=9.0,
        )
        df = self.db.report_summary_dataframe(timestamp, timestamp)
        self.assertEqual(len(df), 1)

    def test_record_cycle(self):
        """Test recording a sleep cycle"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle(
            requested_duration=100,
            active_gpios="GPIO1",
            wakeup_irqs="IRQ1",
            kernel_duration=1.5,
            hw_sleep_duration=2.5,
        )
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT requested, gpio, wake_irq, kernel, hw FROM cycle WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, (100, "GPIO1", "IRQ1", 1.5, 2.5))

    def test_report_debug(self):
        """Test reporting debug messages"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_debug("Test debug message", level=5)
        result = self.db.report_debug(timestamp)
        self.assertEqual(result, [("Test debug message", 5)])

    def test_report_battery(self):
        """Test reporting battery data"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_battery_energy("Battery1", 50, 100, "mWh")
        result = self.db.report_battery(timestamp)
        self.assertEqual(
            result,
            [
                (
                    int(timestamp.strftime("%Y%m%d%H%M%S")),
                    "Battery1",
                    50,
                    None,
                    100,
                    "mWh",
                )
            ],
        )

    def test_record_prereq(self):
        """Test recording a prereq message"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_prereq("Test prereq message", "symbol1")
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT message, symbol FROM prereq_data WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, ("Test prereq message", "symbol1"))

    def test_report_prereq(self):
        """Test reporting prereq messages"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_prereq("Test prereq message 1", "symbol1")
        self.db.record_prereq("Test prereq message 2", "symbol2")
        result = self.db.report_prereq(timestamp)
        self.assertEqual(
            result,
            [
                (
                    int(timestamp.strftime("%Y%m%d%H%M%S")),
                    0,
                    "Test prereq message 1",
                    "symbol1",
                ),
                (
                    int(timestamp.strftime("%Y%m%d%H%M%S")),
                    1,
                    "Test prereq message 2",
                    "symbol2",
                ),
            ],
        )

    def test_report_prereq_no_data(self):
        """Test reporting prereq messages when no data exists"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        result = self.db.report_prereq(timestamp)
        self.assertEqual(result, [])

    def test_report_prereq_none_timestamp(self):
        """Test reporting prereq messages with None timestamp"""
        result = self.db.report_prereq(None)
        self.assertEqual(result, [])

    def test_report_cycle(self):
        """Test reporting a cycle from the database"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle(
            requested_duration=100,
            active_gpios="GPIO1",
            wakeup_irqs="IRQ1",
            kernel_duration=1.5,
            hw_sleep_duration=2.5,
        )
        result = self.db.report_cycle(timestamp)
        self.assertEqual(
            result,
            [
                (
                    int(timestamp.strftime("%Y%m%d%H%M%S")),
                    int(datetime.now().strftime("%Y%m%d%H%M%S")),
                    100,
                    "GPIO1",
                    "IRQ1",
                    1.5,
                    2.5,
                )
            ],
        )

    def test_report_cycle_no_data(self):
        """Test reporting a cycle when no data exists"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        result = self.db.report_cycle(timestamp)
        self.assertEqual(result, [])

    def test_report_cycle_none_timestamp(self):
        """Test reporting a cycle with None timestamp"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle(
            requested_duration=100,
            active_gpios="GPIO1",
            wakeup_irqs="IRQ1",
            kernel_duration=1.5,
            hw_sleep_duration=2.5,
        )
        result = self.db.report_cycle(None)
        self.assertEqual(
            result,
            [
                (
                    int(timestamp.strftime("%Y%m%d%H%M%S")),
                    int(datetime.now().strftime("%Y%m%d%H%M%S")),
                    100,
                    "GPIO1",
                    "IRQ1",
                    1.5,
                    2.5,
                )
            ],
        )

    def test_get_last_cycle(self):
        """Test getting the last cycle from the database"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle(
            requested_duration=100,
            active_gpios="GPIO1",
            wakeup_irqs="IRQ1",
            kernel_duration=1.5,
            hw_sleep_duration=2.5,
        )
        result = self.db.get_last_cycle()
        self.assertEqual(result, (int(timestamp.strftime("%Y%m%d%H%M%S")),))

    def test_get_last_cycle_no_data(self):
        """Test getting the last cycle when no data exists"""
        result = self.db.get_last_cycle()
        self.assertIsNone(result)

    def test_get_last_prereq_ts(self):
        """Test getting the last prereq timestamp from the database"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_prereq("Test prereq message 1", "symbol1")
        self.db.record_prereq("Test prereq message 2", "symbol2")
        result = self.db.get_last_prereq_ts()
        self.assertEqual(result, int(timestamp.strftime("%Y%m%d%H%M%S")))

    def test_get_last_prereq_ts_no_data(self):
        """Test getting the last prereq timestamp when no data exists"""
        result = self.db.get_last_prereq_ts()
        self.assertEqual(result, 0)

    def test_report_cycle_data(self):
        """Test reporting cycle data"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle_data("Test cycle data 1", "symbol1")
        self.db.record_cycle_data("Test cycle data 2", "symbol2")
        result = self.db.report_cycle_data(timestamp)
        expected_result = "symbol1 Test cycle data 1\nsymbol2 Test cycle data 2\n"
        self.assertEqual(result, expected_result)

    def test_report_cycle_data_no_data(self):
        """Test reporting cycle data when no data exists"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        result = self.db.report_cycle_data(timestamp)
        self.assertEqual(result, "")

    def test_report_cycle_data_none_timestamp(self):
        """Test reporting cycle data with None timestamp"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_cycle_data("Test cycle data 1", "symbol1")
        result = self.db.report_cycle_data(None)
        expected_result = "symbol1 Test cycle data 1\n"
        self.assertEqual(result, expected_result)

    def test_record_power_rail_energy_insert(self):
        """Test inserting power rail energy (first call in prep)"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_power_rail_energy("CPU_VDDCR_PH1", 1000000.0, 149011.611)
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT label, e0, e1, scale FROM power_rails WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, ("CPU_VDDCR_PH1", 1000000.0, None, 149011.611))

    def test_record_power_rail_energy_update(self):
        """Test updating power rail energy (second call in post)"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        # First call (prep) - INSERT
        self.db.record_power_rail_energy("CPU_VDDCR_PH1", 1000000.0, 149011.611)
        # Second call (post) - UPDATE
        self.db.record_power_rail_energy("CPU_VDDCR_PH1", 1050000.0, 149011.611)
        cur = self.db.db.cursor()
        cur.execute(
            "SELECT label, e0, e1, scale FROM power_rails WHERE t0=?",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        result = cur.fetchone()
        self.assertEqual(result, ("CPU_VDDCR_PH1", 1000000.0, 1050000.0, 149011.611))

    def test_record_multiple_power_rails(self):
        """Test recording multiple power rails per cycle"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        # Record multiple rails
        rails = [
            ("CPU_VDDCR_PH1", 1000000.0, 1050000.0, 149011.611),
            ("CPU_VDDCR_PH2", 2000000.0, 2100000.0, 149011.611),
            ("VDDIO", 3000000.0, 3150000.0, 23751.3),
        ]
        for label, e0, e1, scale in rails:
            self.db.record_power_rail_energy(label, e0, scale)
            self.db.record_power_rail_energy(label, e1, scale)

        cur = self.db.db.cursor()
        cur.execute(
            "SELECT label, e0, e1, scale FROM power_rails WHERE t0=? ORDER BY label",
            (int(timestamp.strftime("%Y%m%d%H%M%S")),),
        )
        results = cur.fetchall()
        self.assertEqual(len(results), 3)
        self.assertEqual(
            results[0], ("CPU_VDDCR_PH1", 1000000.0, 1050000.0, 149011.611)
        )
        self.assertEqual(
            results[1], ("CPU_VDDCR_PH2", 2000000.0, 2100000.0, 149011.611)
        )
        self.assertEqual(results[2], ("VDDIO", 3000000.0, 3150000.0, 23751.3))

    def test_report_power_rails(self):
        """Test reporting power rails"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        self.db.record_power_rail_energy("CPU_VDDCR_PH1", 1000000.0, 149011.611)
        self.db.record_power_rail_energy("CPU_VDDCR_PH1", 1050000.0, 149011.611)
        result = self.db.report_power_rails(timestamp)
        self.assertEqual(
            result,
            [
                (
                    int(timestamp.strftime("%Y%m%d%H%M%S")),
                    "CPU_VDDCR_PH1",
                    1000000.0,
                    1050000.0,
                    149011.611,
                )
            ],
        )

    def test_report_power_rails_no_data(self):
        """Test reporting power rails when no data exists"""
        timestamp = datetime.now()
        self.db.start_cycle(timestamp)
        result = self.db.report_power_rails(timestamp)
        self.assertEqual(result, [])

    def test_default_database_path_xdg(self):
        """Test default database path uses XDG_DATA_HOME if set"""
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/custom/path"}, clear=True):
            self.assertEqual(default_database_path(), "/custom/path/data.db")

    def test_default_database_path_sudo_user(self):
        """Test default database path uses home directory of SUDO_USER"""
        with patch.dict(os.environ, {"SUDO_USER": "testuser"}, clear=True):
            with patch("os.path.expanduser", return_value="/home/testuser"):
                self.assertEqual(
                    default_database_path(),
                    "/home/testuser/.local/share/amd-debug-tools/data.db",
                )

    def test_default_database_path_unprivileged_user(self):
        """Test default database path uses home directory of unprivileged user"""
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.expanduser", return_value="/home/normaluser"), patch(
                "amd_debug.database.is_root", return_value=False
            ):
                self.assertEqual(
                    default_database_path(),
                    "/home/normaluser/.local/share/amd-debug-tools/data.db",
                )

    @patch("amd_debug.database.is_root", return_value=True)
    def test_default_database_path_root_systemd(self, _mock_root):
        """Test default database path uses /var/lib/amd-s2idle for root/systemd captures"""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(default_database_path(), "/var/lib/amd-s2idle/data.db")

    def test_default_database_path_custom_env(self):
        """Test default database path uses AMD_S2IDLE_DB if set"""
        with patch.dict(os.environ, {"AMD_S2IDLE_DB": "/custom/db.sqlite"}, clear=True):
            self.assertEqual(default_database_path(), "/custom/db.sqlite")

    @patch("amd_debug.database.os.path.islink", return_value=True)
    def test_symlink_path_rejected(self, _mock_islink):
        """Test database rejects symlink in database file or directory"""
        with self.assertRaises(ValueError):
            SleepDatabase(dbf="/tmp/symlink_db.sqlite")

    @patch("amd_debug.database.os.lchown")
    @patch("amd_debug.database.os.makedirs")
    @patch("amd_debug.database.os.path.exists", return_value=False)
    @patch("amd_debug.database.sqlite3.connect", return_value=MagicMock())
    def test_default_db_creates_in_user_data_dir(
        self, _mock_connect, _mock_exists, mock_makedirs, mock_lchown
    ):
        """Test default database creates directory in home/user data dir and chowns if under sudo"""
        with patch.dict(
            os.environ,
            {"SUDO_USER": "testuser", "SUDO_UID": "1000", "SUDO_GID": "1000"},
            clear=True,
        ):
            with patch("os.path.expanduser", return_value="/home/testuser"):
                SleepDatabase()
                mock_makedirs.assert_called_once_with(
                    "/home/testuser/.local/share/amd-debug-tools",
                    mode=0o755,
                    exist_ok=True,
                )
                mock_lchown.assert_any_call(
                    "/home/testuser/.local/share/amd-debug-tools", 1000, 1000
                )

    def test_schema_migration_v1_to_v3(self):
        """Test database migration to the current schema"""
        # Create a new database (will be at the current version)
        db = SleepDatabase(dbf=":memory:")
        cur = db.db.cursor()

        # Verify power_rails table exists
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='power_rails'"
        )
        result = cur.fetchone()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "power_rails")

        # Verify s0ix table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='s0ix'")
        result = cur.fetchone()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "s0ix")

        # Verify schema version is current
        cur.execute("PRAGMA user_version")
        version = cur.fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
