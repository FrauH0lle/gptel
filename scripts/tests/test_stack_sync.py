"""Offline end-to-end Git tests; no third-party Python packages required."""

import dataclasses
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "stack_sync", Path(__file__).resolve().parents[1] / "stack-sync.py")
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class StackIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-stack-sync-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.upstream = self.root / "upstream.git"
        self.fork = self.root / "fork.git"
        self.repo = self.root / "work"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        HOME=str(self.root), GIT_AUTHOR_NAME="Personal Author",
                        GIT_AUTHOR_EMAIL="personal@example.test",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test",
                        GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
                        GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00")
        self.git(None, "init", "--bare", str(self.upstream))
        self.git(None, "init", "--bare", str(self.fork))
        self.git(None, "init", "-b", "master", str(self.repo))
        self.policy = dataclasses.replace(sync.PRODUCTION, fork_url=str(self.fork),
                                          upstream_url=str(self.upstream))
        self.base = self.commit("base.txt", "base\n", "base")
        self.git(self.repo, "push", str(self.upstream), "HEAD:refs/heads/master")
        self.git(self.repo, "checkout", "-b", "pr")
        self.pr = self.commit("pr.txt", "pr v1\n", "PR contribution")
        self.git(self.repo, "push", str(self.upstream), "HEAD:refs/pull/1338/head")
        self.git(self.repo, "checkout", "-b", "pr-1338-layer", self.base)
        self.git(self.repo, "merge", "--squash", self.pr)
        self.git(self.repo, "commit", "-m", self.message(self.base, self.pr))
        self.layer = self.head()
        self.git(self.repo, "checkout", "-b", "local-stack")
        self.stack = self.commit("personal.txt", "personal\n", "Personal change")
        self.push_stack()
        self.candidate = self.root / "candidate"
        self.api_mode = "open"
        self.api_hook = None

    def git(self, repo, *args, check=True):
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false"]
        if repo:
            command += ["-C", str(repo)]
        result = subprocess.run(command + list(args), env=self.env, capture_output=True, text=True)
        if check and result.returncode:
            self.fail(f"{' '.join(args)}: {result.stderr}")
        return result.stdout.strip() if check else result

    def head(self):
        return self.git(self.repo, "rev-parse", "HEAD")

    def message(self, base, pr):
        return (f"PR layer\n\nUpstream-Commit: {base}\nPR-Head: {pr}\n"
                f"PR-URL: {self.policy.pr_url}")

    def commit(self, filename, content, message):
        (self.repo / filename).write_text(content)
        self.git(self.repo, "add", filename)
        self.git(self.repo, "commit", "-m", message)
        return self.head()

    def push_stack(self):
        self.git(self.repo, "push", "--force", str(self.fork),
                 "pr-1338-layer:refs/heads/pr-1338-layer", "local-stack:refs/heads/local-stack")

    def api(self, policy):
        if self.api_hook:
            self.api_hook()
        if self.api_mode == "failure":
            raise sync.StackError("API unavailable")
        if self.api_mode == "malformed":
            return {}
        return dict(state=self.api_mode, merged=False,
                    head=dict(sha=self.git(self.upstream, "rev-parse", "refs/pull/1338/head")))

    def prepare(self, **kwargs):
        return sync.prepare(self.repo, self.candidate, policy=self.policy, provider=self.api, **kwargs)

    def update_upstream(self, filename="upstream.txt", content="upstream v2\n"):
        current = self.git(self.upstream, "rev-parse", "refs/heads/master")
        self.git(self.repo, "checkout", "--detach", current)
        result = self.commit(filename, content, "Upstream update")
        self.git(self.repo, "push", str(self.upstream), "HEAD:refs/heads/master")
        self.git(self.repo, "checkout", "local-stack")
        return result

    def update_pr(self, *, rewrite=False, filename="pr.txt", content="pr v2\n"):
        self.git(self.repo, "checkout", "--detach", self.base if rewrite else self.pr)
        result = self.commit(filename, content, "Rewritten PR" if rewrite else "PR update")
        self.git(self.repo, "push", "--force", str(self.upstream), "HEAD:refs/pull/1338/head")
        self.git(self.repo, "checkout", "local-stack")
        return result

    def refs(self):
        return self.git(self.fork, "show-ref")

    def fake_validation(self, manifest):
        """Inject evidence only for tests that isolate publication mechanics."""
        sha = sync.digest((self.candidate / "manifest.json").read_bytes())
        with sync.verified(self.candidate, policy=self.policy) as (git, bare, _, _, _):
            tree = git.run(bare, "rev-parse", f"{manifest['new']['stack']}^{{tree}}")
        evidence = dict(schema_version=1, success=True, manifest_sha256=sha,
                        bundle_sha256=manifest["bundle_sha256"], new_stack=manifest["new"]["stack"],
                        report=dict(schema_version=1, success=True, head=manifest["new"]["stack"], tree=tree))
        sync.write_json(self.candidate / "validation.json", evidence)

    def publish(self, **kwargs):
        return sync.publish(self.candidate, self.repo, policy=self.policy, provider=self.api, **kwargs)

    def rewrite_manifest(self, change):
        path = self.candidate / "manifest.json"
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))

    def test_update_variants_preserve_authors_and_source_worktree(self):
        for variant in ("upstream", "pr", "rewrite", "both"):
            with self.subTest(variant=variant):
                self.candidate = self.root / variant
                if variant in ("upstream", "both"):
                    self.update_upstream(f"{variant}.txt")
                if variant in ("pr", "rewrite", "both"):
                    self.update_pr(rewrite=variant == "rewrite", content=f"{variant}\n")
                (self.repo / "untracked").write_text("must remain untouched")
                before = self.git(self.repo, "status", "--porcelain"), self.head(), self.git(self.repo, "show-ref")
                manifest = self.prepare()
                self.assertEqual(manifest["status"], "update")
                self.assertEqual(before, (self.git(self.repo, "status", "--porcelain"), self.head(),
                                          self.git(self.repo, "show-ref")))
                with sync.verified(self.candidate, policy=self.policy) as (git, bare, m, _, _):
                    self.assertEqual(git.run(bare, "show", f"{m['new']['stack']}:personal.txt"), "personal")
                    self.assertEqual(git.run(bare, "show", "-s", "--format=%an", m["new"]["stack"]),
                                     "Personal Author")
                    self.assertEqual(git.run(bare, "rev-list", "--count",
                                            f"{m['new']['layer']}..{m['new']['stack']}"), "1")

    def test_noop_checks_live_state_and_does_not_push_or_create_backups(self):
        before = self.refs()
        manifest = self.prepare()
        self.assertEqual(manifest["status"], "noop")
        self.assertEqual(manifest["old"], manifest["new"])
        self.fake_validation(manifest)
        self.publish()
        self.assertEqual(self.refs(), before)
        self.api_mode = "closed"
        self.candidate = self.root / "closed-noop"
        with self.assertRaisesRegex(sync.StackError, "not open"):
            self.prepare()

    def test_publish_atomically_updates_two_branches_and_creates_exact_backups(self):
        self.update_upstream()
        manifest = self.prepare()
        before = self.refs()
        self.fake_validation(manifest)
        sha = sync.digest((self.candidate / "manifest.json").read_bytes())
        self.publish(expected_manifest_sha=sha)
        for tip, branch in (("layer", "pr-1338-layer"), ("stack", "local-stack")):
            self.assertEqual(self.git(self.fork, "rev-parse", f"refs/heads/{branch}"), manifest["new"][tip])
            backup = f"refs/tags/stack-backup/{sha}/{branch}"
            self.assertEqual(self.git(self.fork, "cat-file", "-t", backup), "tag")
            self.assertEqual(self.git(self.fork, "rev-parse", f"{backup}^{{commit}}"),
                             manifest["old"][tip])
        self.assertNotEqual(self.refs(), before)

    def test_local_source_prepare_explicit_and_unpublishable(self):
        self.git(self.fork, "update-ref", "-d", "refs/heads/local-stack")
        with self.assertRaises(sync.StackError):
            self.prepare()
        manifest = self.prepare(local_stack=True)
        self.assertEqual(manifest["source_mode"], "local")
        self.fake_validation(manifest)
        with self.assertRaisesRegex(sync.StackError, "local-source"):
            self.publish()

    def test_missing_branch(self):
        self.git(self.fork, "update-ref", "-d", "refs/heads/pr-1338-layer")
        with self.assertRaises(sync.StackError):
            self.prepare()
        self.assertFalse(self.candidate.exists())

    def test_bad_metadata_and_wrong_parent_rejected_on_noop(self):
        for message in ("missing trailers", self.message(self.base, self.pr).replace("https:", "http:"),
                        self.message(self.base, self.pr) + f"\nPR-Head: {self.pr}",
                        self.message(self.pr, self.pr)):
            with self.subTest(message=message):
                self.git(self.repo, "checkout", "-B", "pr-1338-layer", self.layer)
                self.git(self.repo, "commit", "--amend", "-m", message)
                self.git(self.repo, "branch", "-f", "local-stack", "HEAD")
                self.push_stack()
                with self.assertRaises(sync.StackError):
                    self.prepare()
                self.assertFalse(self.candidate.exists())

    def test_nonlinear_personal_history_rejected(self):
        self.git(self.repo, "checkout", "-b", "side", self.layer)
        self.commit("side.txt", "side", "side")
        self.git(self.repo, "checkout", "local-stack")
        self.git(self.repo, "merge", "--no-ff", "side", "-m", "personal merge")
        self.push_stack()
        with self.assertRaisesRegex(sync.StackError, "contains merges"):
            self.prepare()

    def test_layer_not_ancestor_rejected(self):
        self.git(self.repo, "branch", "-f", "pr-1338-layer", self.base)
        # A valid layer on a separate lineage cannot delimit the personal range.
        self.git(self.repo, "checkout", "pr-1338-layer")
        self.git(self.repo, "commit", "--allow-empty", "-m", self.message(self.base, self.pr))
        self.push_stack()
        with self.assertRaisesRegex(sync.StackError, "not an ancestor"):
            self.prepare()

    def test_closed_merged_malformed_or_failing_api(self):
        for mode in ("closed", "failure", "malformed"):
            with self.subTest(mode=mode):
                self.api_mode = mode
                with self.assertRaises(sync.StackError):
                    self.prepare()
                self.assertFalse(self.candidate.exists())
        with self.assertRaisesRegex(sync.StackError, "not open"):
            sync.prepare(self.repo, self.candidate, policy=self.policy,
                         provider=lambda p: dict(state="open", merged=True, head=dict(sha=self.pr)))

    def test_fetch_failure(self):
        policy = dataclasses.replace(self.policy, upstream_url=str(self.root / "absent.git"))
        with self.assertRaises(sync.StackError):
            sync.prepare(self.repo, self.candidate, policy=policy, provider=self.api)

    def test_merge_conflict_does_not_touch_worktree_or_remote(self):
        self.update_upstream("pr.txt", "conflicting upstream\n")
        before = self.head(), self.refs(), self.git(self.repo, "status", "--porcelain")
        with self.assertRaises(sync.StackError):
            self.prepare()
        self.assertEqual(before, (self.head(), self.refs(), self.git(self.repo, "status", "--porcelain")))
        self.assertFalse(self.candidate.exists())

    def test_personal_rebase_conflict(self):
        self.commit("pr.txt", "personal conflicting PR\n", "personal edits PR")
        self.push_stack()
        self.update_pr()
        before = self.refs()
        with self.assertRaises(sync.StackError):
            self.prepare()
        self.assertEqual(before, self.refs())

    def test_unexpected_empty_stops_but_intentional_empty_survives(self):
        self.update_upstream("personal.txt", "personal\n")
        with self.assertRaises(sync.StackError):
            self.prepare()
        # Remove the nonempty personal patch and replace it with intentional empty.
        self.git(self.repo, "checkout", "local-stack")
        self.git(self.repo, "reset", "--hard", self.layer)
        self.git(self.repo, "commit", "--allow-empty", "-m", "Intentional marker")
        self.push_stack()
        manifest = self.prepare()
        with sync.verified(self.candidate, policy=self.policy) as (git, bare, _, _, _):
            self.assertEqual(git.run(bare, "rev-list", "--count",
                                    f"{manifest['new']['layer']}..{manifest['new']['stack']}"), "1")
            self.assertEqual(git.run(bare, "show", "-s", "--format=%s", manifest["new"]["stack"]),
                             "Intentional marker")

    def test_pr_already_upstream_retains_empty_layer_boundary(self):
        self.git(self.upstream, "update-ref", "refs/heads/master", self.pr)
        manifest = self.prepare()
        with sync.verified(self.candidate, policy=self.policy) as (git, bare, _, _, _):
            self.assertEqual(git.run(bare, "rev-parse", f"{manifest['new']['layer']}^{{tree}}"),
                             git.run(bare, "rev-parse", f"{self.pr}^{{tree}}"))
            self.assertEqual(sync.parents(git, bare, manifest["new"]["layer"]), [self.pr])

    def test_no_personal_commits(self):
        self.git(self.repo, "checkout", "--detach", self.layer)
        self.git(self.repo, "branch", "-f", "local-stack", self.layer)
        self.push_stack()
        self.update_upstream()
        manifest = self.prepare()
        self.assertEqual(manifest["new"]["stack"], manifest["new"]["layer"])

    def test_prepare_detects_input_and_destination_races_even_on_noop(self):
        calls = 0
        def race():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.git(self.fork, "update-ref", "refs/heads/local-stack", self.layer)
        self.api_hook = race
        with self.assertRaisesRegex(sync.StackError, "changed"):
            self.prepare()
        self.assertFalse(self.candidate.exists())

    def test_publish_rejects_changed_sources_destinations_and_closed_pr(self):
        self.update_upstream()
        manifest = self.prepare()
        self.fake_validation(manifest)
        original = self.refs()
        self.api_mode = "closed"
        with self.assertRaises(sync.StackError):
            self.publish()
        self.api_mode = "open"
        self.git(self.fork, "update-ref", "refs/heads/local-stack", self.layer)
        raced = self.refs()
        with self.assertRaisesRegex(sync.StackError, "changed"):
            self.publish()
        self.assertEqual(self.refs(), raced)
        self.git(self.fork, "update-ref", "refs/heads/local-stack", self.stack)
        self.update_pr()
        with self.assertRaisesRegex(sync.StackError, "changed"):
            self.publish()
        self.assertEqual(self.refs(), original)

    def test_atomic_rejection_does_not_partially_update_any_refs(self):
        self.update_upstream()
        manifest = self.prepare()
        self.fake_validation(manifest)
        # Server-side hook, not a mock: rejecting one branch must abort all refs.
        hook = self.fork / "hooks" / "update"
        hook.write_text('#!/bin/sh\n[ "$1" != refs/heads/local-stack ]\n')
        hook.chmod(0o755)
        before = self.refs()
        with self.assertRaisesRegex(sync.StackError, "push failed"):
            self.publish()
        self.assertEqual(self.refs(), before)

    def test_destination_race_at_push_uses_explicit_leases(self):
        self.update_upstream()
        manifest = self.prepare()
        self.fake_validation(manifest)
        original = sync.Git.run
        def raced(git, repo, *args, **kwargs):
            if args[0] == "push":
                self.git(self.fork, "update-ref", "refs/heads/local-stack", self.layer)
            return original(git, repo, *args, **kwargs)
        with mock.patch.object(sync.Git, "run", raced):
            with self.assertRaisesRegex(sync.StackError, "push failed"):
                self.publish()
        self.assertEqual(self.git(self.fork, "rev-parse", "refs/heads/pr-1338-layer"), self.layer)
        self.assertEqual(self.git(self.fork, "rev-parse", "refs/heads/local-stack"), self.layer)
        self.assertFalse(self.git(self.fork, "tag", "--list"))

    def test_existing_backup_must_not_be_overwritten(self):
        self.update_upstream()
        manifest = self.prepare()
        self.fake_validation(manifest)
        sha = sync.digest((self.candidate / "manifest.json").read_bytes())
        backup = f"refs/tags/stack-backup/{sha}/pr-1338-layer"
        self.git(self.fork, "update-ref", backup, self.stack)
        before = self.refs()
        with self.assertRaises(sync.StackError):
            self.publish()
        self.assertEqual(self.refs(), before)

    def test_manifest_and_bundle_tampering_rejected(self):
        self.update_upstream()
        self.prepare()
        original = (self.candidate / "manifest.json").read_bytes()
        for change in (lambda m: m.update(destination="attacker"),
                       lambda m: m.update(schema_version=True),
                       lambda m: m["new"].update(stack="0" * 40),
                       lambda m: m["sources"].update(pr="0" * 40),
                       lambda m: m.update(status="noop"),
                       lambda m: m["old"].update(layer=m["old"]["stack"])):
            with self.subTest(change=change):
                (self.candidate / "manifest.json").write_bytes(original)
                self.rewrite_manifest(change)
                with self.assertRaises(sync.StackError):
                    with sync.verified(self.candidate, policy=self.policy):
                        pass
        (self.candidate / "manifest.json").write_bytes(original)
        with (self.candidate / "candidate.bundle").open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(sync.StackError, "digest"):
            with sync.verified(self.candidate, policy=self.policy):
                pass

    def test_duplicate_json_and_symlink_artifact_rejected(self):
        self.prepare()
        path = self.candidate / "manifest.json"
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(sync.StackError, "duplicate"):
            with sync.verified(self.candidate, policy=self.policy):
                pass
        path.unlink()
        path.symlink_to(self.repo / "base.txt")
        with self.assertRaisesRegex(sync.StackError, "regular file"):
            with sync.verified(self.candidate, policy=self.policy):
                pass

    def test_unexpected_bundle_ref_rejected(self):
        self.prepare()
        with sync.verified(self.candidate, policy=self.policy) as (git, bare, _, _, temp):
            git.run(bare, "update-ref", "refs/heads/unexpected", self.stack)
            other = temp / "other.bundle"
            git.run(bare, "bundle", "create", str(other), "--all")
            (self.candidate / "candidate.bundle").write_bytes(other.read_bytes())
        self.rewrite_manifest(lambda m: m.update(bundle_sha256=sync.digest(
            (self.candidate / "candidate.bundle").read_bytes())))
        with self.assertRaisesRegex(sync.StackError, "allowlist"):
            with sync.verified(self.candidate, policy=self.policy):
                pass

    def test_bundle_requiring_external_objects_and_corrupt_pack_rejected(self):
        self.update_upstream()
        self.prepare()
        full_bundle = (self.candidate / "candidate.bundle").read_bytes()
        with sync.verified(self.candidate, policy=self.policy) as (git, bare, manifest, _, temp):
            thin = temp / "thin.bundle"
            git.run(bare, "bundle", "create", str(thin), *sorted(sync.REFS), f"^{self.base}")
            (self.candidate / "candidate.bundle").write_bytes(thin.read_bytes())
        self.rewrite_manifest(lambda m: m.update(bundle_sha256=sync.digest(
            (self.candidate / "candidate.bundle").read_bytes())))
        with self.assertRaisesRegex(sync.StackError, "bundle failed"):
            with sync.verified(self.candidate, policy=self.policy):
                pass
        bundle = self.candidate / "candidate.bundle"
        bundle.write_bytes(full_bundle[:-20])
        self.rewrite_manifest(lambda m: m.update(bundle_sha256=sync.digest(bundle.read_bytes())))
        with self.assertRaises(sync.StackError):
            with sync.verified(self.candidate, policy=self.policy):
                pass

    def test_rewritten_pr_does_not_require_unavailable_old_raw_commit(self):
        self.update_pr(rewrite=True)
        self.git(self.upstream, "reflog", "expire", "--expire=now", "--all")
        self.git(self.upstream, "prune", "--expire=now")
        self.assertNotEqual(self.git(self.upstream, "cat-file", "-e", self.pr, check=False).returncode, 0)
        manifest = self.prepare()
        self.assertEqual(manifest["old"]["pr"], self.pr)
        with sync.verified(self.candidate, policy=self.policy) as (git, bare, _, _, _):
            self.assertNotEqual(git.run(bare, "cat-file", "-e", self.pr, check=False).returncode, 0)

    def test_existing_same_object_backup_is_also_rejected(self):
        self.update_upstream()
        manifest = self.prepare()
        self.fake_validation(manifest)
        sha = sync.digest((self.candidate / "manifest.json").read_bytes())
        backup = f"refs/tags/stack-backup/{sha}/pr-1338-layer"
        self.git(self.fork, "update-ref", backup, self.layer)
        before = self.refs()
        with self.assertRaisesRegex(sync.StackError, "already exists"):
            self.publish()
        self.assertEqual(self.refs(), before)

    def test_same_commit_backup_created_at_push_cannot_bypass_empty_lease(self):
        self.update_upstream()
        manifest = self.prepare()
        self.fake_validation(manifest)
        sha = sync.digest((self.candidate / "manifest.json").read_bytes())
        backup = f"refs/tags/stack-backup/{sha}/pr-1338-layer"
        original = sync.Git.run
        def raced(git, repo, *args, **kwargs):
            if args[0] == "push":
                self.git(self.fork, "update-ref", backup, self.layer)
            return original(git, repo, *args, **kwargs)
        with mock.patch.object(sync.Git, "run", raced):
            with self.assertRaisesRegex(sync.StackError, "push failed"):
                self.publish()
        self.assertEqual(self.git(self.fork, "rev-parse", "refs/heads/pr-1338-layer"), self.layer)
        self.assertEqual(self.git(self.fork, "rev-parse", "refs/heads/local-stack"), self.stack)
        self.assertEqual(self.git(self.fork, "tag", "--list"), backup.removeprefix("refs/tags/"))

    def test_api_head_mismatch_and_prepare_source_race_fail_closed(self):
        with self.assertRaisesRegex(sync.StackError, "disagree"):
            sync.prepare(self.repo, self.candidate, policy=self.policy,
                         provider=lambda p: dict(state="open", merged=False, head=dict(sha=self.base)))
        calls = 0
        def race():
            nonlocal calls
            calls += 1
            if calls == 1:
                self.update_upstream()
        self.api_hook = race
        with self.assertRaisesRegex(sync.StackError, "upstream changed"):
            self.prepare()

    def test_output_cannot_replace_existing_prepare_artifact(self):
        self.prepare()
        original = (self.candidate / "manifest.json").read_bytes()
        with self.assertRaisesRegex(sync.StackError, "already exist"):
            self.prepare()
        self.assertEqual((self.candidate / "manifest.json").read_bytes(), original)

    def test_publication_requires_bound_validation_and_manifest_digest(self):
        self.update_upstream()
        manifest = self.prepare()
        before = self.refs()
        with self.assertRaises(sync.StackError):
            self.publish()
        self.fake_validation(manifest)
        with self.assertRaisesRegex(sync.StackError, "manifest digest"):
            self.publish(expected_manifest_sha="0" * 64)
        path = self.candidate / "validation.json"
        evidence = json.loads(path.read_text())
        evidence["new_stack"] = self.stack
        path.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(sync.StackError, "binding"):
            self.publish()
        self.assertEqual(before, self.refs())

    def test_trusted_validator_exact_tree_and_immutable_prepare_files(self):
        self.update_upstream()
        manifest = self.prepare()
        original = {name: (self.candidate / name).read_bytes()
                    for name in ("candidate.bundle", "manifest.json")}
        validator = self.root / "trusted-validator.py"
        validator.write_text('''import argparse,json,os,subprocess
p=argparse.ArgumentParser();p.add_argument('--tree');p.add_argument('--report');a=p.parse_args()
assert 'GITHUB_TOKEN' not in os.environ and 'GH_TOKEN' not in os.environ
def git(x):return subprocess.check_output(['git','-C',a.tree,'rev-parse',x],text=True).strip()
with open(a.report,'w') as f:json.dump(dict(schema_version=1,success=True,head=git('HEAD'),tree=git('HEAD^{tree}')),f)
''')
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "must-not-leak", "GH_TOKEN": "must-not-leak"}):
            result = sync.validate(self.candidate, policy=self.policy, validator=validator)
        self.assertEqual(result["new_stack"], manifest["new"]["stack"])
        self.assertEqual(original, {name: (self.candidate / name).read_bytes() for name in original})
        self.publish()

    def test_failed_or_mismatched_validator_cannot_write_success_evidence(self):
        self.prepare()
        validator = self.root / "bad-validator.py"
        for code in ("raise SystemExit(1)",
                     "import sys,json;json.dump(dict(schema_version=1,success=True,head='wrong',tree='wrong'),open(sys.argv[-1],'w'))"):
            validator.write_text(code)
            with self.assertRaises(sync.StackError):
                sync.validate(self.candidate, policy=self.policy, validator=validator)
            self.assertFalse((self.candidate / "validation.json").exists())

    def test_failed_revalidation_preserves_diagnostics_but_removes_stale_success(self):
        manifest = self.prepare()
        self.fake_validation(manifest)
        validator = self.root / "failure-report.py"
        validator.write_text("import sys,json;json.dump(dict(schema_version=1,success=False),open(sys.argv[-1],'w'));sys.exit(1)")
        with self.assertRaisesRegex(sync.StackError, "validation failed"):
            sync.validate(self.candidate, policy=self.policy, validator=validator)
        self.assertFalse((self.candidate / "validation.json").exists())
        self.assertIs(json.loads((self.candidate / "validation-report.json").read_text())["success"], False)

    def test_cli_sync_does_not_publish_by_default_or_execute_noop(self):
        with mock.patch.object(sync, "prepare", return_value={"status": "noop"}), \
                mock.patch.object(sync, "validate") as validate, \
                mock.patch.object(sync, "publish") as publish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(["sync", "--repo", str(self.repo), "--output", str(self.candidate)]), 0)
            validate.assert_not_called()
            publish.assert_not_called()
        with mock.patch.object(sync, "prepare", return_value={"status": "update"}), \
                mock.patch.object(sync, "validate") as validate, \
                mock.patch.object(sync, "publish") as publish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(["sync", "--repo", str(self.repo), "--output", str(self.candidate)]), 0)
            validate.assert_called_once()
            publish.assert_not_called()

    def test_hostile_global_git_config_and_hooks_not_executed(self):
        marker = self.root / "executed"
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        config = self.root / "hostile.gitconfig"
        config.write_text(f"[core]\n hooksPath = {hooks}\n[commit]\n gpgSign = true\n[rebase]\n updateRefs = true\n")
        self.update_upstream()
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config),
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                          "GIT_CONFIG_VALUE_0": str(hooks)}):
            self.prepare()
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
