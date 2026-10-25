#!/usr/bin/env python3
"""Validate a fresh candidate checkout using trusted control-worktree code.

Run ONLY in an unprivileged, disposable job without secrets or Git credentials.
Environment scrubbing is defense in depth, NOT a sandbox for candidate Lisp.
The caller must never invoke this program with a repository writer token.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


TEST_URL = "https://github.com/karthink/gptel-test.git"
TOKEN_VARIABLES = ("GITHUB_TOKEN", "GH_TOKEN", "STACK_SYNC_TOKEN", "GH_ENTERPRISE_TOKEN")
GIT_OPTIONS = [
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
    "-c", "credential.helper=", "-c", "http.extraHeader=",
    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
    "-c", "submodule.recurse=false",
]


class ValidationError(Exception):
    """An operational or validation failure which must block publication."""


def isolated_environment(root):
    """Allowlist environment, rather than guessing every API credential name."""
    root = Path(root)
    home, temp = root / "home", root / "tmp"
    home.mkdir()
    temp.mkdir()
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home), "TMPDIR": str(temp),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "GNUPGHOME": str(home / ".gnupg"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }


class Validator:
    def __init__(self, tree, root, report):
        self.tree = Path(tree).resolve()
        self.root = Path(root)
        self.report = report
        self.env = isolated_environment(root)
        self.loader = Path(__file__).resolve().with_name("stack-check.el")

    def command(self, name, argv, cwd=None):
        check = {"name": name, "status": "fail", "command": list(map(str, argv))}
        self.report["checks"].append(check)
        try:
            result = subprocess.run(
                argv, cwd=cwd or self.root, env=self.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=1200, check=False,
            )
            check.update(exit_code=result.returncode, output=result.stdout)
            if result.returncode:
                raise ValidationError(f"{name} exited {result.returncode}")
            check["status"] = "pass"
            return result.stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            check.update(exit_code=None, output=str(exc))
            raise ValidationError(f"{name}: {exc}") from exc

    def git(self, name, *args, cwd=None):
        return self.command(name, ["git", *GIT_OPTIONS, *args], cwd or self.tree)

    def clean(self, name):
        self.git(name + "-diff", "diff", "--exit-code", "--no-ext-diff",
                 "--no-textconv", "--ignore-submodules=all", "HEAD", "--")
        # Include ignored files: an ignored .elc could otherwise shadow source.
        extra = self.git(name + "-untracked", "ls-files", "--others", "-z")
        if extra:
            raise ValidationError("candidate must be a fresh checkout without untracked files")

    def elisp_files(self, directory, name):
        entries = self.git(name, "ls-tree", "-z", "HEAD", cwd=directory).split("\0")
        files = []
        for entry in filter(None, entries):
            metadata, filename = entry.split("\t", 1)
            if filename.startswith("."):
                continue
            if filename.endswith(".elc"):
                raise ValidationError("tracked bytecode is not allowed in source validation")
            if not filename.endswith(".el"):
                continue
            if metadata.split()[0] not in ("100644", "100755"):
                raise ValidationError(f"non-regular Lisp source: {filename}")
            files.append(str(Path(directory) / filename))
        if not files:
            raise ValidationError(f"no top-level Lisp files found in {name}")
        return sorted(files)

    def emacs(self, mode, config):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        data_path = self.root / (mode + ".json")
        env = self.env.copy()
        env.update(STACK_VALIDATE_CONFIG=str(config_path),
                   STACK_VALIDATE_RESULT=str(data_path), STACK_VALIDATE_MODE=mode)
        self.env, previous = env, self.env
        failed = None
        try:
            self.command(mode, ["emacs", "-Q", "--batch", "--load", str(self.loader)])
        except ValidationError as exc:
            failed = exc
        finally:
            self.env = previous
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("stage result must be a JSON object")
            self.report["dependency_setup" if mode == "dependencies" else mode] = data
            if "emacs" in data:
                self.report["emacs"] = data["emacs"]
            if mode == "dependencies":
                self.report["dependencies"] = data.get("dependencies", [])
                self.report["requirements"] = data.get("requirements", [])
                self.report["test_requirements"] = data.get("test_requirements", [])
            if data.get("status") != "pass":
                failed = ValidationError(f"{mode} reported failure")
        except (OSError, ValueError) as exc:
            failed = ValidationError(f"{mode} produced no valid result: {exc}")
        if failed:
            self.report["checks"][-1]["status"] = "fail"
            raise failed
        return data

    def run(self):
        self.report["checkout"] = str(self.tree)
        self.report["head"] = self.git("candidate-head", "rev-parse", "--verify", "HEAD").strip()
        self.report["tree"] = self.git("candidate-tree", "rev-parse", "HEAD^{tree}").strip()
        self.clean("initial-clean")
        files = self.elisp_files(self.tree, "candidate-lisp")
        gitlink = self.git("test-gitlink", "ls-tree", "HEAD", "--", "test").strip()
        match = re.fullmatch(r"160000 commit ([0-9a-f]{40,64})\ttest", gitlink)
        if not match:
            raise ValidationError("HEAD must record a test submodule gitlink")
        sha = match.group(1)
        tests = self.root / "test"
        self.git("test-init", "init", str(tests), cwd=self.root)
        self.git("test-fetch", "fetch", "--no-tags", "--depth=1", TEST_URL, sha, cwd=tests)
        self.git("test-checkout", "checkout", "--detach", "FETCH_HEAD", cwd=tests)
        actual = self.git("test-head", "rev-parse", "HEAD", cwd=tests).strip()
        if actual != sha:
            raise ValidationError("fetched test HEAD does not match recorded gitlink")
        self.report["submodule"] = {"path": "test", "url": TEST_URL, "head": actual}
        test_files = self.elisp_files(tests, "test-lisp")
        # Keep personal regressions separate from the pinned upstream gitlink.
        local_suite = self.git("local-test-tree", "ls-tree", "HEAD", "--", "local-tests").strip()
        if local_suite:
            if not re.fullmatch(r"040000 tree [0-9a-f]{40,64}\tlocal-tests", local_suite):
                raise ValidationError("local-tests must be a tracked directory")
            test_files += self.elisp_files(self.tree / "local-tests", "local-test-lisp")
        config = {
            "tree": str(self.tree), "tests": str(tests),
            "candidate_files": files, "test_files": test_files,
            "packages": str(self.root / "packages"),
            "compiled": str(self.root / "compiled"),
        }
        self.report["candidate_files"] = files
        self.report["test_files"] = test_files
        deps = self.emacs("dependencies", config)
        if not isinstance(deps.get("emacs"), dict) or deps["emacs"].get("version") != "31.1":
            raise ValidationError("validation requires Emacs 31.1")
        failures = []
        # Do not short-circuit ERT after compilation errors. Source ERT never
        # sees the temporary .elc files produced by the separate process.
        for mode in ("compile", "ert"):
            try:
                data = self.emacs(mode, config)
                if mode == "ert":
                    counts = [data.get(key) for key in
                              ("test_count", "completed", "unexpected", "skipped")]
                    if (not all(type(count) is int for count in counts)
                            or counts[0] <= 0 or counts[1] != counts[0]
                            or counts[2] != 0 or counts[3] != 0):
                        self.report["checks"][-1]["status"] = "fail"
                        data["status"] = "fail"
                        raise ValidationError("ERT did not complete every discovered test successfully")
            except ValidationError as exc:
                failures.append(str(exc))
        self.clean("final-clean")
        if self.git("final-head", "rev-parse", "HEAD").strip() != self.report["head"]:
            failures.append("candidate HEAD changed during validation")
        if self.git("final-tree", "rev-parse", "HEAD^{tree}").strip() != self.report["tree"]:
            failures.append("candidate tree changed during validation")
        if failures:
            raise ValidationError("; ".join(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    tree, destination = args.tree.resolve(), args.report.resolve()
    if destination.is_relative_to(tree):
        parser.error("report must be outside the candidate source tree")
    report = {"schema_version": 1, "success": False, "checks": []}
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise ValidationError("refusing to execute candidate code as root")
        if any(os.environ.get(name) for name in TOKEN_VARIABLES):
            raise ValidationError("refusing to run with a GitHub token in the environment")
        with tempfile.TemporaryDirectory(prefix="gptel-stack-validate-") as temporary:
            Validator(tree, temporary, report).run()
        report["success"] = True
    except (ValidationError, OSError, ValueError, KeyError) as exc:
        report["error"] = str(exc)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"cannot write validation report: {exc}", file=sys.stderr)
        return 1
    if not report["success"]:
        print(report.get("error", "validation failed"), file=sys.stderr)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
