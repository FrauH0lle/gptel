"""Offline regression tests for the trusted validation runner (stdlib only)."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "stack_validate", Path(__file__).resolve().parents[1] / "stack-validate.py")
validate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate)
HEAD = "1" * 40
TREE = "2" * 40
TEST = "3" * 40


class FakeProcesses:
    """Fixture process runner: never executes Git, Emacs, or network code."""

    def __init__(self):
        self.calls = []
        self.stage_data = {}
        self.stage_exits = {}
        self.missing_result = set()
        self.dirty = False
        self.untracked = ""
        self.test_head = TEST
        self.test_files = ["gptel-helper.el", "strangely-named.el"]
        self.gitlink_mode = "160000"

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        env = kwargs["env"]
        code, output = 0, ""
        if argv[0] == "emacs":
            mode = env["STACK_VALIDATE_MODE"]
            data = {
                "status": "pass", "emacs": {"version": "31.1"},
                "dependencies": [{"name": "compat", "version": "30.1.0.0",
                                  "loaded_paths": ["/isolated/compat.elc"]}],
                "requirements": [{"name": "emacs", "minimum": "27.1"}],
            }
            if mode == "ert":
                data.update(test_count=2, completed=2, unexpected=0, skipped=0)
            data.update(self.stage_data.get(mode, {}))
            if mode not in self.missing_result:
                Path(env["STACK_VALIDATE_RESULT"]).write_text(json.dumps(data))
            code = self.stage_exits.get(mode, 0)
        else:
            args = argv[1 + len(validate.GIT_OPTIONS):]
            if args[0] == "rev-parse":
                if args[-1] == "HEAD^{tree}":
                    output = TREE + "\n"
                elif Path(kwargs["cwd"]).name == "test":
                    output = self.test_head + "\n"
                else:
                    output = HEAD + "\n"
            elif args[0] == "diff" and self.dirty:
                code = 1
            elif args[0] == "ls-files":
                output = self.untracked
            elif args[0] == "ls-tree":
                if args[-1] == "test":
                    output = f"{self.gitlink_mode} commit {TEST}\ttest\n"
                else:
                    files = (self.test_files if Path(kwargs["cwd"]).name == "test"
                             else [".dir-locals.el", "gptel.el", "gptel-request.el"])
                    output = "".join(f"100644 blob {TREE}\t{name}\0" for name in files)
        return subprocess.CompletedProcess(argv, code, output)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "candidate"
        self.tree.mkdir()
        self.report_path = self.root / "report.json"
        self.fake = FakeProcesses()

    def run_validation(self):
        with mock.patch.object(validate.subprocess, "run", side_effect=self.fake), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin",
                                          "OPENAI_API_KEY": "secret",
                                          "AWS_SECRET_ACCESS_KEY": "secret"}, clear=True), \
             contextlib.redirect_stderr(io.StringIO()):
            status = validate.main(["--tree", str(self.tree),
                                    "--report", str(self.report_path)])
        return status, json.loads(self.report_path.read_text())

    def test_success_records_identity_all_files_and_allowlisted_fetch(self):
        status, report = self.run_validation()
        self.assertEqual(status, 0)
        self.assertTrue(report["success"])
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual((report["head"], report["tree"]), (HEAD, TREE))
        self.assertEqual(report["submodule"], {
            "path": "test", "url": validate.TEST_URL, "head": TEST})
        self.assertEqual([Path(p).name for p in report["test_files"]], self.fake.test_files)
        self.assertNotIn(".dir-locals.el", [Path(p).name for p in report["candidate_files"]])
        fetches = [argv for argv, _ in self.fake.calls if "fetch" in argv]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][-2:], [validate.TEST_URL, TEST])
        self.assertFalse(any("submodule" in argv for argv, _ in self.fake.calls))

    def test_all_children_have_isolated_environment_and_trusted_loader(self):
        self.run_validation()
        for argv, kwargs in self.fake.calls:
            env = kwargs["env"]
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotEqual(env["HOME"], os.environ["HOME"])
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            if argv[0] == "emacs":
                self.assertIn("-Q", argv)
                self.assertEqual(Path(argv[-1]), Path(validate.__file__).with_name("stack-check.el"))
                self.assertFalse(Path(env["STACK_VALIDATE_RESULT"]).is_relative_to(self.tree))

    def test_compile_failure_still_runs_source_ert_and_blocks_success(self):
        self.fake.stage_exits["compile"] = 1
        self.fake.stage_data["compile"] = {"status": "fail", "error": "existing compiler error"}
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertFalse(report["success"])
        self.assertEqual(report["ert"]["test_count"], 2)
        stages = [kwargs["env"]["STACK_VALIDATE_MODE"] for argv, kwargs in self.fake.calls
                  if argv[0] == "emacs"]
        self.assertEqual(stages, ["dependencies", "compile", "ert"])

    def test_nonzero_ert_blocks_even_with_forged_passing_metadata(self):
        self.fake.stage_exits["ert"] = 1
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertFalse(report["success"])

    def test_zero_missing_incomplete_skipped_and_failing_tests_block(self):
        for data in ({"test_count": 0}, {"completed": 1}, {"skipped": 1},
                     {"unexpected": 1}, {"status": "fail"}, {"test_count": "2"},
                     {"skipped": None}):
            with self.subTest(data=data):
                self.fake.stage_data["ert"] = data
                status, report = self.run_validation()
                self.assertEqual(status, 1)
                self.assertFalse(report["success"])

    def test_missing_emacs_result_blocks(self):
        self.fake.missing_result.add("ert")
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertIn("no valid result", report["error"])

    def test_dependency_failure_does_not_run_candidate_code(self):
        self.fake.stage_exits["dependencies"] = 1
        self.fake.stage_data["dependencies"] = {"status": "fail", "error": "archive unavailable"}
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertNotIn("ert", report)
        self.assertEqual(report["emacs"]["version"], "31.1")

    def test_initial_emacs_version_enforced(self):
        self.fake.stage_data["dependencies"] = {"emacs": {"version": "30.2"}}
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertIn("31.1", report["error"])

    def test_test_checkout_must_match_gitlink(self):
        self.fake.test_head = "4" * 40
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertIn("gitlink", report["error"])
        self.assertNotIn("ert", report)

    def test_missing_gitlink_and_empty_suite_block(self):
        self.fake.gitlink_mode = "100644"
        self.assertEqual(self.run_validation()[0], 1)
        self.fake.gitlink_mode = "160000"
        self.fake.test_files = []
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertIn("no top-level Lisp", report["error"])

    def test_dirty_and_ignored_untracked_files_block(self):
        self.fake.dirty = True
        self.assertEqual(self.run_validation()[0], 1)
        self.fake.dirty = False
        self.fake.untracked = "gptel.elc\0"
        self.assertEqual(self.run_validation()[0], 1)

    def test_source_bytecode_is_rejected(self):
        self.fake.test_files = ["suite.elc", "suite.el"]
        status, report = self.run_validation()
        self.assertEqual(status, 1)
        self.assertIn("bytecode", report["error"])

    def test_git_process_failure_and_timeout_produce_failure_reports(self):
        for error in (FileNotFoundError("git unavailable"), subprocess.TimeoutExpired("git", 1)):
            with self.subTest(error=error), \
                 mock.patch.object(validate.subprocess, "run", side_effect=error), \
                 mock.patch.dict(os.environ, {}, clear=True), \
                 contextlib.redirect_stderr(io.StringIO()):
                status = validate.main(["--tree", str(self.tree), "--report", str(self.report_path)])
                self.assertEqual(status, 1)
                self.assertFalse(json.loads(self.report_path.read_text())["success"])

    def test_github_tokens_refused_before_any_process(self):
        for token in validate.TOKEN_VARIABLES:
            with self.subTest(token=token), \
                 mock.patch.object(validate.subprocess, "run") as process, \
                 mock.patch.dict(os.environ, {token: "writer-token"}, clear=True), \
                 contextlib.redirect_stderr(io.StringIO()):
                status = validate.main(["--tree", str(self.tree), "--report", str(self.report_path)])
                self.assertEqual(status, 1)
                process.assert_not_called()
                self.assertIn("token", json.loads(self.report_path.read_text())["error"])

    def test_privileged_execution_refused_before_any_process(self):
        with mock.patch.object(validate.os, "geteuid", return_value=0), \
             mock.patch.object(validate.subprocess, "run") as process, \
             contextlib.redirect_stderr(io.StringIO()):
            status = validate.main(["--tree", str(self.tree), "--report", str(self.report_path)])
        self.assertEqual(status, 1)
        process.assert_not_called()
        self.assertIn("root", json.loads(self.report_path.read_text())["error"])

    def test_report_inside_candidate_is_rejected_without_writing(self):
        destination = self.tree / "report.json"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            validate.main(["--tree", str(self.tree), "--report", str(destination)])
        self.assertEqual(exc.exception.code, 2)
        self.assertFalse(destination.exists())


class LoaderTests(unittest.TestCase):
    """Exercise the real installed Emacs against tiny offline fixtures only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stack-loader-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("candidate", "test", "packages", "compiled", "home"):
            (self.root / name).mkdir()
        self.source = self.root / "candidate" / "fixture.el"
        self.source.write_text(";;; fixture.el -*- lexical-binding: t; -*-\n"
                               "(defun fixture-answer () 42)\n(provide 'fixture)\n")
        self.suite = self.root / "test" / "unusual-name.el"
        self.suite.write_text(";;; suite.el -*- lexical-binding: t; -*-\n"
                              "(require 'ert)\n(require 'fixture)\n"
                              "(ert-deftest fixture-pass () (should (= (fixture-answer) 42)))\n")
        config = {
            "tree": str(self.source.parent), "tests": str(self.suite.parent),
            "candidate_files": [str(self.source)], "test_files": [str(self.suite)],
            "packages": str(self.root / "packages"), "compiled": str(self.root / "compiled"),
        }
        (self.root / "config.json").write_text(json.dumps(config))

    def run_loader(self, mode, prelude=None):
        env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.root / "home"),
            "STACK_VALIDATE_CONFIG": str(self.root / "config.json"),
            "STACK_VALIDATE_RESULT": str(self.root / (mode + ".json")),
            "STACK_VALIDATE_MODE": mode,
        }
        argv = ["emacs", "-Q", "--batch"]
        if prelude:
            argv.extend(["--load", str(prelude)])
        argv.extend(["--load", str(Path(validate.__file__).with_name("stack-check.el"))])
        result = subprocess.run(
            argv,
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30, check=False,
        )
        return result, json.loads((self.root / (mode + ".json")).read_text())

    def test_compilation_is_outside_source_and_ert_uses_source(self):
        result, data = self.run_loader("compile")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["status"], "pass")
        self.assertTrue((self.root / "compiled" / "fixture.elc").exists())
        self.assertFalse(list(self.source.parent.glob("*.elc")))
        self.source.write_text(self.source.read_text().replace("42", "43"))
        self.suite.write_text(self.suite.read_text().replace("42", "43"))
        result, data = self.run_loader("ert")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["test_count"], 1)
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["unexpected"], 0)

    def test_byte_compile_error_is_not_suppressed(self):
        self.source.write_text(";;; fixture.el -*- lexical-binding: t; -*-\n"
                               "(eval-when-compile (error \"fixture compiler error\"))\n")
        result, data = self.run_loader("compile")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["files"][0]["status"], "fail")

    def test_no_ert_discovery_is_explicit_failure(self):
        self.suite.write_text("(require 'ert)\n")
        result, data = self.run_loader("ert")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["test_count"], 0)
        self.assertIn("No ERT tests", data["error"])

    def test_full_ert_failure_has_nonzero_exit_and_counts(self):
        self.suite.write_text(self.suite.read_text().replace("42", "0"))
        result, data = self.run_loader("ert")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["test_count"], 1)
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["unexpected"], 1)
        self.assertEqual(data["status"], "fail")

    def test_skipped_ert_cannot_report_success(self):
        self.suite.write_text("(require 'ert)\n(ert-deftest fixture-skip () (ert-skip \"fixture\"))\n")
        _, data = self.run_loader("ert")
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(data["status"], "fail")

    def test_stable_archives_cover_direct_and_transitive_dependencies(self):
        # Real GNU Transient 0.13.8 introduced NonGNU cond-let/llama deps.
        # Fake archive transport/install keeps this regression entirely offline,
        # but exercises the trusted Package-Requires parser and archive policy.
        (self.source.parent / "gptel.el").write_text(
            ";;; gptel.el --- fixture -*- lexical-binding: t; -*-\n"
            ";; Version: 1.0\n"
            ';; Package-Requires: ((emacs "27.1") (transient "0.7.8") (compat "30.1.0.0"))\n'
            ";;; Commentary:\n;; Fixture.\n;;; Code:\n(provide 'gptel)\n")
        prelude = self.root / "archive-fixture.el"
        prelude.write_text(r''';;; archive-fixture.el -*- lexical-binding: t; -*-
(require 'cl-lib)
(require 'package)
(defun package-refresh-contents ()
  (unless (equal package-archives
                 '(("gnu" . "https://elpa.gnu.org/packages/")
                   ("nongnu" . "https://elpa.nongnu.org/nongnu/")))
    (error "Only stable GNU and NonGNU HTTPS archives cover the fixture"))
  (setq package-archive-contents nil)
  (dolist (fixture '((transient (0 13 8) ((cond-let (1 1)) (llama (1 0))) "gnu")
                     (compat (30 1 0 0) nil "gnu")
                     (cond-let (1 1) nil "nongnu")
                     (llama (1 0) nil "nongnu")
                     (markdown-mode (2 8) nil "nongnu")))
    (push (list (nth 0 fixture)
                (package-desc-create :name (nth 0 fixture)
                                     :version (nth 1 fixture)
                                     :reqs (nth 2 fixture)
                                     :archive (nth 3 fixture)
                                     :summary "fixture" :kind 'single))
          package-archive-contents)))
(defun package-install (desc &optional _dont-select)
  (unless (assq (package-desc-name desc) package-alist)
    (dolist (req (package-desc-reqs desc))
      (let ((dependency (cadr (assq (car req) package-archive-contents))))
        (unless (and dependency
                     (version-list-<= (cadr req) (package-desc-version dependency)))
          (error "Missing transitive fixture dependency %S" req))
        (package-install dependency)))
    (let* ((name (package-desc-name desc))
           (directory (expand-file-name (symbol-name name) package-user-dir))
           (source (expand-file-name (concat (symbol-name name) ".el") directory)))
      (make-directory directory t)
      (setf (package-desc-dir desc) directory)
      (with-temp-file source
        (insert ";;; fixture -*- lexical-binding: t; -*-\n")
        (dolist (req (package-desc-reqs desc))
          (insert (format "(require '%S)\n" (car req))))
        (insert (format "(provide '%S)\n" name)))
      (push (list name desc) package-alist)
      (add-to-list 'load-path directory))))
''')
        result, data = self.run_loader("dependencies", prelude)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["status"], "pass")
        packages = {item["name"]: item for item in data["dependencies"]}
        self.assertEqual(set(packages), {"transient", "compat", "cond-let", "llama", "markdown-mode"})
        self.assertEqual(data["test_requirements"], [{"name": "markdown-mode", "minimum": "0"}])
        for package in packages.values():
            self.assertTrue(package["loaded_paths"], package)
            self.assertTrue(Path(package["library"]).is_relative_to(self.root / "packages"))
        self.assertEqual(packages["transient"]["version"], "0.13.8")
        self.assertEqual(packages["cond-let"]["archive"], "nongnu")
        self.assertEqual(packages["llama"]["archive"], "nongnu")


if __name__ == "__main__":
    unittest.main()
