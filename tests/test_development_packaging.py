from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
PKGBUILD = ROOT / "packaging" / "development" / "PKGBUILD"
README = ROOT / "README.md"


class DevelopmentPackagingTests(unittest.TestCase):
    def test_vcs_package_installs_the_desktop_integration(self) -> None:
        package = PKGBUILD.read_text(encoding="utf-8")

        self.assertIn("pkgname=immich-on-demand-git", package)
        self.assertIn("provides=(\"immich-on-demand=$pkgver\")", package)
        self.assertIn("conflicts=('immich-on-demand')", package)
        self.assertIn("git+https://github.com/totalolage/immich-on-demand.git", package)
        self.assertIn("b2sums=('SKIP')", package)
        for dependency in (
            "gtk4",
            "hicolor-icon-theme",
            "libadwaita",
            "libsecret",
            "nautilus-python",
            "org.freedesktop.secrets",
            "python-httpx",
            "python-pillow",
            "python-gobject",
            "python-pyfuse3",
            "python-secretstorage",
            "python-trio",
        ):
            self.assertIn(f"'{dependency}'", package)
        for destination in (
            "/usr/share/applications/net.kalny.ImmichOnDemand.desktop",
            "/usr/share/nautilus-python/extensions/immich-on-demand.py",
            "/usr/share/icons/hicolor/scalable/apps/immich-on-demand.svg",
            "/usr/share/icons/hicolor/scalable/emblems",
            "/usr/lib/systemd/user/immich-on-demand.service",
            "/usr/lib/systemd/user/immich-on-demand@.service",
        ):
            self.assertIn(destination, package)
        self.assertIn("git clean -dfx", package)
        self.assertIn(
            'PYTHONPATH="$PWD/src" python -m unittest discover -s tests', package
        )
        self.assertIn(
            "desktop-file-validate packaging/net.kalny.ImmichOnDemand.desktop",
            package,
        )
        self.assertIn("checkdepends=('desktop-file-utils')", package)
        self.assertIn("for emblem in busy cached pinned recoverable", package)
        self.assertNotIn("systemctl", package)
        self.assertNotIn("sudo", package)

        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "systemctl --user disable --now immich-on-demand@home.service", readme
        )
        self.assertIn("sudo pacman -Rns immich-on-demand-git", readme)


if __name__ == "__main__":
    unittest.main()
