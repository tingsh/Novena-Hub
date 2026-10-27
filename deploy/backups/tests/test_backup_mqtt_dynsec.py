import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "backup_mqtt_dynsec.sh"


class BackupMqttDynsecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "dynamic-security.json"
        self.backups = self.root / "backups"

    def run_backup(self):
        env = os.environ.copy()
        env.update(
            MQTT_DYNSEC_SOURCE=str(self.source),
            MQTT_DYNSEC_BACKUP_DIR=str(self.backups),
            MQTT_DYNSEC_BACKUP_RCLONE_REMOTE="",
        )
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_valid_state_is_saved_privately(self):
        state = {"clients": [{"username": "gateway-test"}], "roles": []}
        self.source.write_text(json.dumps(state))
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = list((self.backups / "daily").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text()), state)
        self.assertEqual(saved[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)

    def test_invalid_state_does_not_publish_backup(self):
        self.source.write_text('{"clients": [}')
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((self.backups / "daily").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
