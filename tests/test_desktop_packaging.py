import configparser
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
from zipfile import ZipFile


ROOT = Path(__file__).parents[1]
DESKTOP_FILE = ROOT / "packaging" / "immich-on-demand.desktop"
ICON_DIRECTORY = ROOT / "packaging" / "icons"
ICON_NAMES = {
    "immich-on-demand",
    "immich-on-demand-busy",
    "immich-on-demand-cached",
    "immich-on-demand-pinned",
    "immich-on-demand-recoverable",
}


def _load_desktop_module():
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")
    gi.require_version = lambda _namespace, _version: None
    repository.Adw = types.SimpleNamespace(Application=object)
    repository.Gio = types.SimpleNamespace(
        ApplicationFlags=types.SimpleNamespace(HANDLES_COMMAND_LINE=1)
    )
    repository.GLib = types.SimpleNamespace()
    repository.Gtk = types.SimpleNamespace()
    gi.repository = repository
    sys.modules.pop("immich_on_demand.desktop_app", None)
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        return importlib.import_module("immich_on_demand.desktop_app")


class DesktopPackagingTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("immich_on_demand.desktop_app", None)

    def test_project_and_wheel_export_the_desktop_executable(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["scripts"]["immich-on-demand-desktop"],
            "immich_on_demand.desktop_app:main",
        )
        self.assertTrue(callable(_load_desktop_module().main))

        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--no-isolation",
                    "--outdir",
                    directory,
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            wheel = next(Path(directory).glob("*.whl"))
            with ZipFile(wheel) as archive:
                self.assertIn(
                    "immich_on_demand/uploads.py", archive.namelist()
                )
                entry_points_name = next(
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/entry_points.txt")
                )
                entry_points = archive.read(entry_points_name).decode("utf-8")
        self.assertIn(
            "immich-on-demand-desktop=immich_on_demand.desktop_app:main",
            entry_points.replace(" ", ""),
        )

    def test_desktop_entry_uses_fixed_executable_and_icon_names(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        with DESKTOP_FILE.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        entry = parser["Desktop Entry"]

        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "Immich On-Demand")
        self.assertEqual(entry["Exec"], "immich-on-demand-desktop")
        self.assertEqual(entry["TryExec"], "immich-on-demand-desktop")
        self.assertEqual(entry["Icon"], "immich-on-demand")
        self.assertEqual(entry["Terminal"], "false")
        self.assertNotIn("%", entry["Exec"])
        self.assertIn("Settings", entry["Categories"].split(";"))

    def test_icons_are_small_self_contained_scalable_svgs(self) -> None:
        icons = {path.stem: path for path in ICON_DIRECTORY.glob("*.svg")}
        self.assertEqual(set(icons), ICON_NAMES)

        for name, path in icons.items():
            with self.subTest(icon=name):
                self.assertLess(path.stat().st_size, 8 * 1024)
                root = ET.parse(path).getroot()
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertRegex(root.attrib["viewBox"], r"^0 0 [1-9][0-9]* [1-9][0-9]*$")
                titles = root.findall("{http://www.w3.org/2000/svg}title")
                self.assertEqual(len(titles), 1)
                self.assertTrue(titles[0].text)
                self.assertEqual(
                    root.findall(".//{http://www.w3.org/2000/svg}script"), []
                )
                self.assertEqual(
                    root.findall(".//{http://www.w3.org/2000/svg}image"), []
                )
                for element in root.iter():
                    for attribute, value in element.attrib.items():
                        self.assertNotIn("href", attribute)
                        self.assertNotIn("url(", value)


if __name__ == "__main__":
    unittest.main()
