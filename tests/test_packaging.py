from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
PKGBUILD = ROOT / "packaging" / "PKGBUILD"
SERVICE = ROOT / "packaging" / "immich-on-demand.service"
SERVICE_TEMPLATE = ROOT / "packaging" / "immich-on-demand@.service"


class PackagingTests(unittest.TestCase):
    def test_pkgbuild_uses_distribution_dependencies_and_pep517_wheel(self) -> None:
        package = PKGBUILD.read_text(encoding="utf-8")

        self.assertIn("pkgver=1.0.0", package)
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
        self.assertRegex(package, r"b2sums=\('[0-9a-f]{128}'\)")
        self.assertNotIn("SKIP", package)
        self.assertNotIn("nautilus-python", package)

    def test_services_run_profiled_unprivileged_foreground_processes(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        template = SERVICE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(
            "ExecStart=/usr/bin/immich-on-demand --profile default mount", service
        )
        self.assertIn("ExecStart=/usr/bin/immich-on-demand --profile %i mount", template)
        self.assertNotIn("%I", template)
        for unit in (service, template):
            self.assertIn("Type=exec", unit)
            self.assertIn("KillSignal=SIGINT", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("RestartPreventExitStatus=78", unit)
            self.assertIn("UMask=0077", unit)
            self.assertNotIn("User=root", unit)
            for forbidden in ("sudo", "docker", "podman", "systemd-sysusers"):
                self.assertNotIn(forbidden, unit.lower())

if __name__ == "__main__":
    unittest.main()
