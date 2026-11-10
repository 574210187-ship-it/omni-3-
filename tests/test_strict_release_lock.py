import unittest

from benchmark_av_sync.stage2_contract import (
    CONTRACT_VERSION,
    STRICT_PACKAGE_VERSION,
    STRICT_RELEASE_ID,
    Stage2ContractError,
)
from benchmark_av_sync.stage2_release import (
    validate_installed_release,
    validate_job_release,
)


class StrictReleaseLockTests(unittest.TestCase):
    def test_installed_release_is_hash_locked(self):
        release = validate_installed_release()
        self.assertEqual(release["release_id"], STRICT_RELEASE_ID)
        self.assertEqual(release["contract"], CONTRACT_VERSION)
        self.assertEqual(release["package_version"], STRICT_PACKAGE_VERSION)
        self.assertRegex(release["manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_job_must_name_exact_installed_release(self):
        installed = validate_installed_release()
        self.assertEqual(validate_job_release(dict(installed), installed=installed), installed)
        stale = dict(installed)
        stale["manifest_sha256"] = "0" * 64
        with self.assertRaises(Stage2ContractError):
            validate_job_release(stale, installed=installed)


if __name__ == "__main__":
    unittest.main()
