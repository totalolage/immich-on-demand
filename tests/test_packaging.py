from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
PKGBUILD = ROOT / "packaging" / "PKGBUILD"
SERVICE = ROOT / "packaging" / "immich-on-demand.service"


class PackagingTests(unittest.TestCase):
    def test_pkgbuild_uses_distribution_dependencies_and_pep517_wheel(self) -> None:
        package = PKGBUILD.read_text(encoding="utf-8")

        for dependency in (
            "libsecret",
            "org.freedesktop.secrets",
            "python-httpx",
            "python-pillow",
            "python-gobject",
            "python-pyfuse3",
            "python-secretstorage",
            "python-trio",
        ):
            self.assertIn(f"'{dependency}'", package)
        for build_dependency in ("python-build", "python-flit-core", "python-installer"):
            self.assertIn(f"'{build_dependency}'", package)
        self.assertIn("python -m build --wheel --no-isolation", package)
        self.assertIn('python -m installer --destdir="$pkgdir"', package)
        self.assertIn("license=('GPL-3.0-or-later')", package)
        self.assertIn("arch=('any')", package)
        self.assertIn("/usr/lib/systemd/user/immich-on-demand.service", package)
        self.assertNotIn("nautilus-python", package)

    def test_service_runs_unprivileged_foreground_process(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")

        self.assertIn("Type=exec", service)
        self.assertIn("ExecStart=/usr/bin/immich-on-demand mount", service)
        self.assertIn("KillSignal=SIGINT", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("UMask=0077", service)
        self.assertNotIn("User=root", service)
        for forbidden in ("sudo", "docker", "podman", "systemd-sysusers"):
            self.assertNotIn(forbidden, service.lower())

if __name__ == "__main__":
    unittest.main()
