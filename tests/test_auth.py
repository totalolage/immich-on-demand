from pathlib import Path
import unittest

from immich_on_demand.auth import api_key_permissions
from immich_on_demand.immich import MUTATION_PERMISSIONS, READ_PERMISSIONS, UPLOAD_PERMISSIONS
from immich_on_demand.settings import Settings


class AuthTest(unittest.TestCase):
    def test_api_key_permissions_follow_the_profile_policy(self) -> None:
        read_only = Settings("https://photos.example.test", Path("/Photos"))
        destructive = Settings(
            "https://photos.example.test",
            Path("/Photos"),
            remote_delete=True,
        )

        self.assertEqual(api_key_permissions(read_only, "read-only"), READ_PERMISSIONS)
        self.assertEqual(api_key_permissions(read_only, "mutation"), UPLOAD_PERMISSIONS)
        self.assertEqual(api_key_permissions(destructive, "mutation"), MUTATION_PERMISSIONS)


if __name__ == "__main__":
    unittest.main()
