"""Offline checks for backup publication and off-server verification wiring."""

import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        binaries = self.path / "bin"
        binaries.mkdir()
        docker = binaries / "docker"
        docker.write_text(
            '#!/bin/sh\ncase " $* " in\n'
            '  *" pg_dump "*) printf "PGDMPtest-archive" ;;\n'
            '  *" pg_restore --list "*) cat >/dev/null ;;\n'
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        docker.chmod(0o755)
        rclone = binaries / "rclone"
        rclone.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = config ]; then\n'
            '  if [ "$3" = novena_r2_crypt ]; then printf "type = crypt\\n"; else printf "type = s3\\n"; fi\n'
            "  exit 0\n"
            "fi\n"
            'printf "%s\\n" "$1" >> "$RCLONE_CALLS"\n'
        )
        rclone.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "RCLONE_CALLS": str(self.path / "rclone-calls"),
            "NOVENA_ROOT": str(self.path),
        }

    def run_script(self, name, **values):
        return subprocess.run(
            ["bash", str(ROOT / "deploy" / "backups" / name)],
            env={**self.env, **values},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_compose_database_backup_is_private_and_remote_is_verified(self):
        backup_dir = self.path / "postgres-backups"
        result = self.run_script(
            "backup_postgres.sh",
            NOVENA_BACKUP_DB_MODE="compose",
            NOVENA_BACKUP_DIR=str(backup_dir),
            BACKUP_RCLONE_REMOTE="novena_r2_crypt:postgres",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list((backup_dir / "daily").glob("*.dump.gz"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual((self.path / "rclone-calls").read_text().splitlines(), ["copy", "cryptcheck"])

    def test_private_media_backup_archives_files_without_exposing_them(self):
        media = self.path / "private-media"
        media.mkdir()
        (media / "customer-report.pdf").write_bytes(b"private report")
        backup_dir = self.path / "media-backups"
        result = self.run_script(
            "backup_private_media.sh",
            NOVENA_PRIVATE_MEDIA_DIR=str(media),
            NOVENA_PRIVATE_MEDIA_BACKUP_DIR=str(backup_dir),
            PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE="novena_r2_crypt:private-media",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list((backup_dir / "daily").glob("*.tar.gz"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o600)
        with tarfile.open(archives[0], "r:gz") as archive:
            self.assertIn("./customer-report.pdf", archive.getnames())
        self.assertEqual((self.path / "rclone-calls").read_text().splitlines(), ["copy", "cryptcheck"])

    def test_missing_private_media_fails_without_creating_empty_backup(self):
        backup_dir = self.path / "media-backups"
        result = self.run_script(
            "backup_private_media.sh",
            NOVENA_PRIVATE_MEDIA_DIR=str(self.path / "missing"),
            NOVENA_PRIVATE_MEDIA_BACKUP_DIR=str(backup_dir),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(backup_dir.exists())

    def test_plain_r2_remote_is_rejected_before_upload(self):
        media = self.path / "private-media"
        media.mkdir()
        result = self.run_script(
            "backup_private_media.sh",
            NOVENA_PRIVATE_MEDIA_DIR=str(media),
            NOVENA_PRIVATE_MEDIA_BACKUP_DIR=str(self.path / "media-backups"),
            PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE="novena_r2_raw:novenaplatform-backup",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("upload skipped", result.stderr)
        self.assertFalse((self.path / "rclone-calls").exists())

    def test_required_offsite_destination_cannot_be_omitted(self):
        result = self.run_script(
            "backup_postgres.sh",
            NOVENA_BACKUP_DB_MODE="compose",
            NOVENA_OFFSITE_REQUIRED="1",
            NOVENA_BACKUP_DIR=str(self.path / "postgres-backups"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required", result.stderr)
        self.assertFalse((self.path / "postgres-backups").exists())


if __name__ == "__main__":
    unittest.main()
