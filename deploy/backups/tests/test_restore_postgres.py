"""Exercise the guarded restore flow without connecting to a database."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "restore_postgres.sh"


class RestorePostgresTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.log = root / "calls.log"
        self.backup = root / "backup.dump.gz"
        self.backup.write_bytes(b"test archive placeholder")

        tools = {
            "psql": """#!/bin/sh
printf 'psql:%s\\n' "$*" >> "$MOCK_LOG"
case "$*" in
  *'SELECT count(*) FROM pg_tables'*) printf '%s\\n' "${MOCK_TABLES:-0}" ;;
  *"SELECT 1 FROM pg_roles"*)
    if [ "${MOCK_ROLE_EXISTS:-0}" = 1 ]; then printf '1\\n'; fi ;;
esac
""",
            "gzip": """#!/bin/sh
printf 'gzip:%s\\n' "$*" >> "$MOCK_LOG"
printf 'fake custom archive'
""",
            "pg_restore": """#!/bin/sh
printf 'pg_restore:%s\\n' "$*" >> "$MOCK_LOG"
cat >/dev/null
if [ "${MOCK_RESTORE_FAIL:-0}" = 1 ]; then exit 1; fi
""",
        }
        for name, source in tools.items():
            path = bin_dir / name
            path.write_text(source)
            path.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{bin_dir}:{self.env['PATH']}",
                "MOCK_LOG": str(self.log),
                "BACKUP_FILE": str(self.backup),
                "RESTORE_DATABASE_URL": "postgresql://admin@localhost/restore_test",
                "RESTORE_CONFIRM": "restore-now",
            }
        )
        self.env.pop("DATABASE_URL", None)

    def run_restore(self, **changes):
        env = self.env.copy()
        env.update({key: str(value) for key, value in changes.items()})
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def test_missing_role_is_created_before_timescale_restore(self):
        result, calls = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        joined = "\n".join(calls)
        self.assertLess(joined.index("CREATE ROLE novena LOGIN"), joined.index("timescaledb_pre_restore"))
        self.assertLess(joined.index("timescaledb_pre_restore"), joined.index("pg_restore:"))
        self.assertLess(joined.index("pg_restore:"), joined.index("timescaledb_post_restore"))
        self.assertIn("--exit-on-error", joined)
        self.assertNotIn("--clean", joined)

    def test_populated_target_is_rejected_before_mutation(self):
        result, calls = self.run_restore(MOCK_TABLES=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("create an empty database", result.stderr)
        self.assertFalse(any("CREATE ROLE" in call or call.startswith("pg_restore:") for call in calls))

    def test_existing_role_is_not_recreated(self):
        result, calls = self.run_restore(MOCK_ROLE_EXISTS=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("CREATE ROLE" in call for call in calls))
        self.assertTrue(any(call.startswith("pg_restore:") for call in calls))

    def test_restore_failure_exits_and_clears_timescale_restore_mode(self):
        result, calls = self.run_restore(MOCK_RESTORE_FAIL=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("timescaledb_post_restore" in call for call in calls))

    def test_target_url_must_be_explicit(self):
        result, calls = self.run_restore(RESTORE_DATABASE_URL="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RESTORE_DATABASE_URL is required", result.stderr)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
