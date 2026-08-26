from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
DEBIAN = ROOT / "debian"


class DebianPackagingTests(unittest.TestCase):
    def test_ubuntu_candidate_uses_native_dependencies_and_inert_user_units(self) -> None:
        control = (DEBIAN / "control").read_text(encoding="utf-8")
        rules = (DEBIAN / "rules").read_text(encoding="utf-8")
        install = (DEBIAN / "immich-on-demand.install").read_text(encoding="utf-8")

        self.assertIn("debhelper-compat (= 13)", control)
        self.assertIn("flit (>= 3.11)", control)
        self.assertIn("pybuild-plugin-pyproject", control)
        for dependency in (
            "gir1.2-adw-1",
            "gir1.2-gtk-4.0",
            "gir1.2-nautilus-4.1",
            "fuse3",
            "gnome-keyring",
            "hicolor-icon-theme",
            "nautilus (>= 1:50)",
            "nautilus (<< 1:51)",
            "python3-httpx",
            "python3-pil",
            "python3-gi",
            "python3-pyfuse3",
            "python3-nautilus (<< 4.2)",
            "python3-secretstorage",
            "python3-trio",
            "systemd",
        ):
            self.assertIn(dependency, control)

        self.assertIn("dh $@ --with python3 --buildsystem=pybuild", rules)
        self.assertIn("export PYBUILD_SYSTEM=pyproject", rules)
        self.assertIn("dh_installsystemduser --no-enable", rules)
        self.assertIn("python3 -m unittest discover -s tests", rules)
        self.assertIn("desktop-file-validate", rules)
        self.assertNotIn("pip", control + rules)
        self.assertNotIn("venv", control + rules)
        self.assertNotIn("systemctl", rules)

        postinst = (DEBIAN / "immich-on-demand.postinst").read_text(
            encoding="utf-8"
        )
        prerm = (DEBIAN / "immich-on-demand.prerm").read_text(encoding="utf-8")
        postrm = (DEBIAN / "immich-on-demand.postrm").read_text(encoding="utf-8")
        self.assertIn('test -n "$2"', postinst)
        self.assertIn("abort-upgrade|abort-deconfigure|abort-remove", postinst)
        self.assertIn("try-restart", postinst)
        self.assertIn('systemctl --quiet --user --machine "$uid@"', postinst)
        self.assertNotIn("deb-systemd-invoke --user restart", postinst)
        self.assertIn("remove|deconfigure", prerm)
        self.assertIn("deb-systemd-invoke --user stop", prerm)
        self.assertIn("deb-systemd-invoke --user daemon-reload", postrm)
        for script in (postinst, prerm):
            self.assertIn("'immich-on-demand@*.service'", script)
            self.assertIn("immich-on-demand.service", script)
        for script in (postinst, prerm, postrm):
            self.assertIn("#DEBHELPER#", script)
            self.assertIn('test -z "${DPKG_ROOT:-}"', script)

        for source in (
            "packaging/immich-on-demand.service",
            "packaging/immich-on-demand@.service",
            "packaging/net.kalny.ImmichOnDemand.desktop",
            "packaging/immich-on-demand-nautilus.py",
            "packaging/icons/immich-on-demand.svg",
        ):
            self.assertIn(source, install)

        self.assertEqual(
            (DEBIAN / "source" / "format").read_text(encoding="utf-8"),
            "3.0 (quilt)\n",
        )
        self.assertIn(
            "immich-on-demand (2.0.0~dev0-1) UNRELEASED",
            (DEBIAN / "changelog").read_text(encoding="utf-8"),
        )

    def test_maintainer_scripts_do_nothing_for_an_alternate_root(self) -> None:
        environment = {"DPKG_ROOT": "/image", "PATH": "/nonexistent"}
        for name, arguments in (
            ("postinst", ("configure", "1.0.0")),
            ("prerm", ("remove",)),
            ("postrm", ("remove",)),
        ):
            result = subprocess.run(
                ["/bin/sh", str(DEBIAN / f"immich-on-demand.{name}"), *arguments],
                env=environment,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, name)
            self.assertEqual(result.stderr, b"", name)

    def test_postinst_try_restarts_loaded_profiles_only_after_an_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "systemd"
            runtime.mkdir()
            calls = root / "calls"
            for command, body in (
                (
                    "deb-systemd-invoke",
                    '#!/bin/sh\nprintf "helper %s\\n" "$*" >> "$CALLS"\n',
                ),
                (
                    "systemctl",
                    "#!/bin/sh\n"
                    'printf "systemctl %s\\n" "$*" >> "$CALLS"\n'
                    'case "$*" in *list-units*) '
                    "printf '%s\\n' 'user@1000.service loaded active running' "
                    "'invalid.service loaded active running';; esac\n",
                ),
            ):
                executable = root / command
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)
            policy = root / "policy-rc.d"
            policy.write_text(
                "#!/bin/sh\n"
                'printf "policy %s\\n" "$*" >> "$CALLS"\n'
                'exit "${POLICY_STATUS:-0}"\n',
                encoding="utf-8",
            )
            policy.chmod(0o755)
            postinst = root / "postinst"
            postinst.write_text(
                (DEBIAN / "immich-on-demand.postinst")
                .read_text(encoding="utf-8")
                .replace("/run/systemd/system", str(runtime))
                .replace("/usr/sbin/policy-rc.d", str(policy)),
                encoding="utf-8",
            )
            environment = os.environ | {
                "CALLS": str(calls),
                "DPKG_ROOT": "",
                "PATH": f"{root}:{os.environ['PATH']}",
            }

            subprocess.run(
                ["/bin/sh", str(postinst), "configure"],
                env=environment,
                check=True,
            )
            self.assertFalse(calls.exists())
            for arguments in (
                ("configure", "1.0.0"),
                ("abort-upgrade",),
                ("abort-deconfigure",),
                ("abort-remove",),
            ):
                subprocess.run(
                    ["/bin/sh", str(postinst), *arguments],
                    env=environment,
                    check=True,
                )
            lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 24)
            self.assertEqual(
                lines[4],
                "systemctl --quiet --user --machine 1000@ try-restart -- "
                "immich-on-demand@*.service",
            )
            self.assertNotIn("invalid@", "\n".join(lines))

            calls.write_text("", encoding="utf-8")
            environment["POLICY_STATUS"] = "101"
            subprocess.run(
                ["/bin/sh", str(postinst), "abort-remove"],
                env=environment,
                check=True,
            )
            denied_lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(denied_lines), 3)
            self.assertFalse(any(line.startswith("systemctl") for line in denied_lines))


if __name__ == "__main__":
    unittest.main()
