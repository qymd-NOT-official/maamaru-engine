import re
import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "maamaru.iss"
RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
INSTALLER_SMOKE = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test_installer.ps1"


class InstallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = INSTALLER.read_text(encoding="utf-8")

    def test_installs_per_user_without_elevation(self):
        self.assertIn(r"DefaultDirName={localappdata}\Programs\Maamaru", self.script)
        self.assertIn("PrivilegesRequired=lowest", self.script)

    def test_installer_only_packages_program_files(self):
        sources = re.findall(r'^Source:\s*"([^"]+)"', self.script, flags=re.MULTILINE)
        self.assertEqual(
            sources,
            [
                r"{#PackageDir}\まあ丸启动器.exe",
                r"{#PackageDir}\manifest.json",
                r"{#PackageDir}\update-manifest.json",
                r"{#PackageDir}\_internal\*",
            ],
        )

    def test_shortcuts_are_registered_to_the_installed_launcher(self):
        self.assertIn(r'Name: "{group}\まあ丸"; Filename: "{app}\まあ丸启动器.exe"', self.script)
        self.assertIn(r'Name: "{autodesktop}\まあ丸"; Filename: "{app}\まあ丸启动器.exe"', self.script)
        self.assertIn('Flags: unchecked', self.script)

    def test_uninstall_only_deletes_user_data_after_explicit_guarded_choice(self):
        self.assertNotRegex(self.script, r"(?im)^\[UninstallDelete\]\s*$")
        self.assertNotRegex(self.script, r"(?im)^Type:\s*filesandordirs")
        self.assertNotRegex(self.script, r"(?im)^Name:\s*.*\\Maamaru(?:\\|\s*$)")
        self.assertIn("AskUninstallPurpose", self.script)
        self.assertIn("换盘、重装或稍后再用", self.script)
        self.assertIn("不再使用まあ丸", self.script)
        self.assertIn("DeleteUserData", self.script)
        self.assertIn(".maamaru-relocation.json", self.script)
        self.assertIn("IsSameOrParent", self.script)
        self.assertIn("ExpandConstant('{userprofile}')", self.script)
        self.assertIn("确定继续吗？", self.script)

    def test_silent_uninstall_skips_prompt_and_keeps_user_data(self):
        # 2026-08-23 事故：自定义卸载询问窗不受 /VERYSILENT 抑制，
        # CI 冒烟在卸载步骤永久挂起。静默模式必须直接走保留数据分支。
        init = re.search(
            r"function InitializeUninstall\(\): Boolean;(?P<body>.*?)\nend;",
            self.script, flags=re.DOTALL)
        self.assertIsNotNone(init)
        body = init.group("body")
        silent = re.search(
            r"if UninstallSilent then(.*?)Exit;", body, flags=re.DOTALL)
        self.assertIsNotNone(silent, "静默卸载必须跳过自定义询问窗")
        self.assertIn("DeleteUserData := False", silent.group(1))
        self.assertLess(body.index("if UninstallSilent then"),
                        body.index("AskUninstallPurpose();"))

    def test_installer_uses_simplified_chinese_messages(self):
        language = INSTALLER.with_name("ChineseSimplified.isl")
        self.assertIn(r'MessagesFile: ".\ChineseSimplified.isl"', self.script)
        self.assertTrue(language.is_file())
        self.assertIn("LanguageName=简体中文", language.read_text(encoding="utf-8"))

    def test_release_smoke_runs_before_publishing(self):
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        smoke_step = "验证安装、首次启动与卸载"
        publish_step = "发布 Release"
        self.assertIn(smoke_step, workflow)
        self.assertIn("scripts/smoke_test_installer.ps1", workflow)
        self.assertLess(workflow.index(smoke_step), workflow.index(publish_step))
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)

    def test_installer_smoke_covers_clean_install_and_data_survival(self):
        script = INSTALLER_SMOKE.read_text(encoding="utf-8")
        for required in (
            "/DIR=",
            "--panel",
            "http://127.0.0.1:8080/api/status",
            "installer-smoke-preserve.txt",
            "uninstall_preserved_user_data",
            "WaitForExit",  # 安装/卸载等待必须有超时兜底，禁止挂死 CI
        ):
            self.assertIn(required, script)


if __name__ == "__main__":
    unittest.main()
