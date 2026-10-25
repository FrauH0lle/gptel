#!/usr/bin/env python3
"""Prepare, validate, and explicitly publish the fixed gptel PR #1338 stack.

Preparation files (manifest.json and candidate.bundle) are immutable inputs to
the other stages. Validation is deliberately separate: it executes candidate
code and must run without publication credentials. Local evidence is not a
security boundary; CI must additionally gate publication on the validation job
and bind the original preparation artifact by ID and manifest digest.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
import urllib.request
import uuid


class StackError(Exception):
    """A failed invariant; no publication is permitted."""


@dataclass(frozen=True)
class Policy:
    """Injection seam for offline integration tests, not a CLI option."""

    fork_url: str = "https://github.com/FrauH0lle/gptel.git"
    upstream_url: str = "https://github.com/karthink/gptel.git"
    upstream_ref: str = "refs/heads/master"
    pr_ref: str = "refs/pull/1338/head"
    layer_ref: str = "refs/heads/pr-1338-layer"
    stack_ref: str = "refs/heads/local-stack"
    pr_url: str = "https://github.com/karthink/gptel/pull/1338"
    api_url: str = "https://api.github.com/repos/karthink/gptel/pulls/1338"


PRODUCTION = Policy()
OID = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TIPS = {"upstream", "pr", "layer", "stack"}
REFS = {f"refs/stack-sync/{age}-{tip}" for age in ("old", "new") for tip in TIPS
        if (age, tip) != ("old", "pr")}


def require(condition, message):
    if not condition:
        raise StackError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), f"not a regular file: {path}")
    return path.read_bytes()


def json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(data):
    try:
        return json.loads(data, object_pairs_hook=json_pairs)
    except (ValueError, UnicodeError) as exc:
        raise StackError(f"invalid JSON: {exc}") from exc


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


class Git:
    """Git in a disposable HOME with no inherited configuration or executors."""

    def __init__(self, home):
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("GIT_") and k not in
                    {"HOME", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN"}}
        self.env.update(HOME=str(home), XDG_CONFIG_HOME=str(home),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="gptel stack synchronizer",
                        GIT_AUTHOR_EMAIL="stack-sync@users.noreply.github.com",
                        GIT_COMMITTER_NAME="gptel stack synchronizer",
                        GIT_COMMITTER_EMAIL="stack-sync@users.noreply.github.com",
                        GIT_EDITOR="false", GIT_SEQUENCE_EDITOR="false",
                        LC_ALL="C")
        self.config = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                       "-c", "tag.gpgSign=false", "-c", "rerere.enabled=false",
                       "-c", "rebase.updateRefs=false", "-c", "credential.helper=",
                       "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=always",
                       "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            # Environment config keeps the credential out of process arguments.
            self.env.update(GIT_CONFIG_COUNT="1",
                            GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
                            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {auth}")

    def run(self, repo, *args, check=True):
        command = ["git", *self.config]
        if repo is not None:
            command += ["-C", str(repo)]
        result = subprocess.run(command + list(args), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        if check and result.returncode:
            raise StackError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip() if check else result

    def init(self, path, bare=False):
        self.run(None, "init", "--object-format=sha1", *(["--bare"] if bare else []),
                 "--template=", str(path))

    def oid(self, repo, ref):
        value = self.run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        require(OID.fullmatch(value), "non-SHA1 repository is unsupported")
        return value

    def fetch(self, repo, remote, ref, name):
        self.run(repo, "fetch", "--no-tags", "--no-recurse-submodules", remote,
                 f"{ref}:refs/stack-sync/{name}")
        return self.oid(repo, f"refs/stack-sync/{name}")


def api_state(policy):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gptel-stack-sync",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(policy.api_url, headers=headers),
                                    timeout=30) as response:
            return load_json(response.read())
    except Exception as exc:
        raise StackError(f"PR API request failed: {exc}") from exc


def check_pr(policy, provider, expected):
    try:
        state = provider(policy)
        require(state["state"] == "open" and state["merged"] is False,
                "PR is not open and unmerged; manual review required")
        require(state["head"]["sha"] == expected, "PR API and fetched PR head disagree")
    except (KeyError, TypeError) as exc:
        raise StackError("malformed PR API response") from exc


def parents(git, repo, commit):
    return git.run(repo, "show", "-s", "--format=%P", commit).split()


def layer_metadata(git, repo, layer, policy):
    message = git.run(repo, "show", "-s", "--format=%B", layer)
    # Require an unambiguous final trailer block, including exact URL and hashes.
    block = message.split("\n\n")[-1].splitlines()
    values = {}
    for line in block:
        match = re.fullmatch(r"(Upstream-Commit|PR-Head|PR-URL): (\S+)", line)
        require(match is not None, "malformed layer trailer block")
        key, value = match.groups()
        require(key not in values, "duplicate layer trailer")
        values[key] = value
    require(set(values) == {"Upstream-Commit", "PR-Head", "PR-URL"},
            "missing layer metadata")
    require(OID.fullmatch(values["Upstream-Commit"]) and OID.fullmatch(values["PR-Head"]),
            "invalid layer source SHA")
    require(values["PR-URL"] == policy.pr_url, "unexpected PR URL")
    require(parents(git, repo, layer) == [values["Upstream-Commit"]],
            "layer must have exactly its recorded upstream parent")
    return values


def linear(git, repo, layer, stack):
    result = git.run(repo, "merge-base", "--is-ancestor", layer, stack, check=False)
    require(result.returncode == 0, "layer is not an ancestor of stack")
    require(not git.run(repo, "rev-list", "--merges", f"{layer}..{stack}"),
            "personal stack contains merges")


def remote_oid(git, repo, remote, ref):
    lines = git.run(repo, "ls-remote", "--refs", remote, ref).splitlines()
    require(len(lines) == 1, f"missing or ambiguous remote ref: {ref}")
    oid, actual = lines[0].split("\t")
    require(actual == ref and OID.fullmatch(oid), f"unexpected remote ref: {ref}")
    return oid


def check_live(git, repo, manifest, policy, provider, local_repo=None):
    require(remote_oid(git, repo, policy.upstream_url, policy.upstream_ref) ==
            manifest["sources"]["upstream"], "upstream changed during preparation/publication")
    require(remote_oid(git, repo, policy.upstream_url, policy.pr_ref) ==
            manifest["sources"]["pr"], "PR head changed during preparation/publication")
    check_pr(policy, provider, manifest["sources"]["pr"])
    source = str(local_repo) if manifest["source_mode"] == "local" else policy.fork_url
    for tip, ref in (("layer", policy.layer_ref), ("stack", policy.stack_ref)):
        require(remote_oid(git, repo, source, ref) == manifest["old"][tip],
                f"destination/source {tip} changed")


def prepare(repo, output, *, local_stack=False, policy=PRODUCTION, provider=api_state):
    repo, output = Path(repo).resolve(), Path(output).absolute()
    require(repo.is_dir(), "repository does not exist")
    require(not output.exists(), "output must not already exist")
    with tempfile.TemporaryDirectory(prefix="stack-sync-") as temp:
        temp = Path(temp)
        git, scratch = Git(temp), temp / "scratch"
        git.init(scratch)
        upstream = git.fetch(scratch, policy.upstream_url, policy.upstream_ref, "new-upstream")
        pr = git.fetch(scratch, policy.upstream_url, policy.pr_ref, "new-pr")
        check_pr(policy, provider, pr)
        source = str(repo) if local_stack else policy.fork_url
        layer = git.fetch(scratch, source, policy.layer_ref, "old-layer")
        stack = git.fetch(scratch, source, policy.stack_ref, "old-stack")
        metadata = layer_metadata(git, scratch, layer, policy)
        old = dict(upstream=metadata["Upstream-Commit"], pr=metadata["PR-Head"],
                   layer=layer, stack=stack)
        linear(git, scratch, layer, stack)
        unchanged = (upstream == old["upstream"] and pr == old["pr"])
        new = dict(upstream=upstream, pr=pr, layer=layer, stack=stack)
        if not unchanged:
            git.run(scratch, "checkout", "--detach", upstream)
            git.run(scratch, "merge", "--squash", "--no-commit", pr)
            message = ("Squashed contribution from PR #1338\n\n"
                       f"Upstream-Commit: {upstream}\nPR-Head: {pr}\nPR-URL: {policy.pr_url}\n")
            git.run(scratch, "commit", "--allow-empty", "-m", message)
            new["layer"] = git.oid(scratch, "HEAD")
            git.run(scratch, "checkout", "--detach", stack)
            git.run(scratch, "rebase", "--merge", "--empty=stop", "--keep-empty",
                    "--reapply-cherry-picks", "--no-update-refs", "--onto", new["layer"], layer)
            new["stack"] = git.oid(scratch, "HEAD")
        manifest = dict(schema_version=1, source_mode="local" if local_stack else "remote",
                        status="noop" if unchanged else "update",
                        sources=dict(upstream=upstream, pr=pr), old=old, new=new)
        check_live(git, scratch, manifest, policy, provider, repo)
        for age, tips in (("old", old), ("new", new)):
            for tip, oid in tips.items():
                ref = f"refs/stack-sync/{age}-{tip}"
                if ref in REFS:
                    git.run(scratch, "update-ref", ref, oid)
        bundle = temp / "candidate.bundle"
        git.run(scratch, "bundle", "create", str(bundle), *sorted(REFS))
        manifest["bundle_sha256"] = digest(bundle.read_bytes())
        # Do not leave a plausible artifact if any precondition failed.
        output.mkdir(parents=True)
        shutil.copyfile(bundle, output / "candidate.bundle")
        write_json(output / "manifest.json", manifest)
        return manifest


def manifest_schema(manifest):
    require(isinstance(manifest, dict) and set(manifest) ==
            {"schema_version", "source_mode", "status", "sources", "old", "new", "bundle_sha256"},
            "unknown or missing manifest fields")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1,
            "unsupported manifest version")
    require(manifest["source_mode"] in ("remote", "local"), "invalid source mode")
    require(manifest["status"] in ("noop", "update"), "invalid status")
    require(isinstance(manifest["bundle_sha256"], str) and
            DIGEST.fullmatch(manifest["bundle_sha256"]), "invalid bundle digest")
    for key, names in (("sources", {"upstream", "pr"}), ("old", TIPS), ("new", TIPS)):
        values = manifest[key]
        require(isinstance(values, dict) and set(values) == names, f"invalid {key} fields")
        require(all(isinstance(v, str) and OID.fullmatch(v) for v in values.values()),
                f"invalid {key} SHA")
    require(manifest["sources"] == {k: manifest["new"][k] for k in ("upstream", "pr")},
            "source/candidate mismatch")
    unchanged = all(manifest["old"][k] == manifest["new"][k] for k in ("upstream", "pr"))
    require((manifest["status"] == "noop") == unchanged, "incorrect no-op status")
    if unchanged:
        require(manifest["old"] == manifest["new"], "no-op changes candidate tips")


@contextlib.contextmanager
def verified(candidate, *, policy=PRODUCTION, expected_manifest_sha=None):
    candidate = Path(candidate).resolve()
    raw = read_regular(candidate / "manifest.json")
    manifest_sha = digest(raw)
    if expected_manifest_sha is not None:
        require(DIGEST.fullmatch(expected_manifest_sha) and manifest_sha == expected_manifest_sha,
                "manifest digest mismatch")
    manifest = load_json(raw)
    manifest_schema(manifest)
    bundle_data = read_regular(candidate / "candidate.bundle")
    require(digest(bundle_data) == manifest["bundle_sha256"], "bundle digest mismatch")
    with tempfile.TemporaryDirectory(prefix="stack-verify-") as temp:
        temp = Path(temp)
        bundle, bare = temp / "candidate.bundle", temp / "objects.git"
        bundle.write_bytes(bundle_data)  # Stable copy prevents artifact TOCTOU.
        git = Git(temp)
        git.init(bare, bare=True)
        git.run(bare, "bundle", "verify", str(bundle))  # Reject prerequisites.
        heads = git.run(bare, "bundle", "list-heads", str(bundle)).splitlines()
        expected = {f"refs/stack-sync/{age}-{tip}": oid
                    for age in ("old", "new") for tip, oid in manifest[age].items()
                    if f"refs/stack-sync/{age}-{tip}" in REFS}
        actual = {}
        for line in heads:
            oid, ref = line.split(" ", 1)
            require(ref not in actual, "duplicate bundle ref")
            actual[ref] = oid
        require(actual == expected, "bundle refs do not match exact allowlist and manifest")
        git.run(bare, "fetch", "--no-tags", "--no-recurse-submodules", str(bundle),
                *[f"{ref}:{ref}" for ref in sorted(REFS)])
        git.run(bare, "fsck", "--strict", "--full", "--no-reflogs")
        for age in ("old", "new"):
            tips = manifest[age]
            require(all(git.oid(bare, oid) == oid for tip, oid in tips.items()
                        if (age, tip) != ("old", "pr")), "non-commit tip")
            meta = layer_metadata(git, bare, tips["layer"], policy)
            require(meta["Upstream-Commit"] == tips["upstream"] and
                    meta["PR-Head"] == tips["pr"], "layer source metadata mismatch")
            linear(git, bare, tips["layer"], tips["stack"])
        # Rebase may change committer/time/parents, but never author or messages.
        def commits(tips):
            return git.run(bare, "log", "--reverse", "--format=%an%x00%ae%x00%aI%x00%B%x00",
                           f"{tips['layer']}..{tips['stack']}")
        require(commits(manifest["old"]) == commits(manifest["new"]),
                "personal commit boundaries/authorship changed")
        yield git, bare, manifest, manifest_sha, temp


def validate(candidate, *, policy=PRODUCTION, validator=None):
    candidate = Path(candidate).resolve()
    evidence_path = candidate / "validation.json"
    if evidence_path.exists() or evidence_path.is_symlink():
        require(not evidence_path.is_symlink() and evidence_path.is_file(),
                "existing validation evidence is not a regular file")
        evidence_path.unlink()  # A failed retry must not leave prior success.
    validator = Path(validator) if validator else Path(__file__).resolve().with_name("stack-validate.py")
    with verified(candidate, policy=policy) as (git, bare, manifest, manifest_sha, temp):
        tree, report = temp / "tree", temp / "report.json"
        git.init(tree)
        git.run(tree, "fetch", "--no-tags", "--no-recurse-submodules", str(bare),
                "refs/stack-sync/new-stack")
        git.run(tree, "checkout", "--detach", manifest["new"]["stack"])
        # Strip publication tokens/headers from the child's environment. This is
        # not a same-host sandbox: CI's fresh jobs provide the credential boundary.
        env = {k: v for k, v in git.env.items() if not k.startswith("GIT_CONFIG_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        result = subprocess.run([sys.executable, str(validator), "--tree", str(tree),
                                 "--report", str(report)], env=env)
        if report.is_file() and not report.is_symlink():
            # Diagnostics survive failure, but never authorize publication.
            diagnostic = candidate / "validation-report.json"
            require(not diagnostic.is_symlink(), "diagnostic report must not be a symlink")
            diagnostic.write_bytes(report.read_bytes())
        require(result.returncode == 0, "candidate validation failed")
        evidence = load_json(read_regular(report))
        require(isinstance(evidence, dict) and type(evidence.get("schema_version")) is int and
                evidence.get("schema_version") == 1 and
                evidence.get("success") is True and evidence.get("head") == manifest["new"]["stack"] and
                evidence.get("tree") == git.run(bare, "rev-parse", f"{manifest['new']['stack']}^{{tree}}"),
                "validator report does not identify successful exact candidate")
        bound = dict(schema_version=1, success=True, manifest_sha256=manifest_sha,
                     bundle_sha256=manifest["bundle_sha256"], new_stack=manifest["new"]["stack"],
                     report=evidence)
        write_json(candidate / "validation.json", bound)
        return bound


def validation_binding(candidate, manifest, manifest_sha, git, bare):
    evidence = load_json(read_regular(Path(candidate) / "validation.json"))
    require(isinstance(evidence, dict) and set(evidence) ==
            {"schema_version", "success", "manifest_sha256", "bundle_sha256", "new_stack", "report"},
            "invalid validation evidence schema")
    require(type(evidence["schema_version"]) is int and evidence["schema_version"] == 1 and
            evidence["success"] is True and evidence["manifest_sha256"] == manifest_sha and
            evidence["bundle_sha256"] == manifest["bundle_sha256"] and
            evidence["new_stack"] == manifest["new"]["stack"], "validation binding mismatch")
    report = evidence["report"]
    require(isinstance(report, dict) and type(report.get("schema_version")) is int and
            report.get("schema_version") == 1 and
            report.get("success") is True and report.get("head") == manifest["new"]["stack"] and
            report.get("tree") == git.run(bare, "rev-parse", f"{manifest['new']['stack']}^{{tree}}"),
            "validation report mismatch")


def publish(candidate, repo, *, expected_manifest_sha=None, policy=PRODUCTION, provider=api_state):
    require(Path(repo).is_dir(), "repository does not exist")
    with verified(candidate, policy=policy, expected_manifest_sha=expected_manifest_sha) as data:
        git, bare, manifest, manifest_sha, _ = data
        require(manifest["source_mode"] == "remote", "local-source candidates cannot be published")
        validation_binding(candidate, manifest, manifest_sha, git, bare)
        if manifest["status"] == "noop":
            check_live(git, bare, manifest, policy, provider)
            return manifest
        specs, leases = [], []
        for tip, ref in (("layer", policy.layer_ref), ("stack", policy.stack_ref)):
            specs.append(f"{manifest['new'][tip]}:{ref}")
            leases.append(f"--force-with-lease={ref}:{manifest['old'][tip]}")
            backup = f"refs/tags/stack-backup/{manifest_sha}/{ref.rsplit('/', 1)[1]}"
            require(not git.run(bare, "ls-remote", "--refs", policy.fork_url, backup),
                    "backup tag already exists")
            # Git skips an identical advertised object as 'up to date' without
            # testing its lease. A fresh annotated object makes even a racing
            # lightweight backup of this same commit a real create-only update.
            git.run(bare, "tag", "--annotate", "--no-sign", "--message",
                    f"Stack backup for manifest {manifest_sha}\nNonce: {uuid.uuid4().hex}",
                    backup.removeprefix("refs/tags/"), manifest["old"][tip])
            tag_oid = git.run(bare, "rev-parse", "--verify", backup)
            specs.append(f"{tag_oid}:{backup}")
            leases.append(f"--force-with-lease={backup}:")
        check_live(git, bare, manifest, policy, provider)
        # Leases reject differing destination values at push, not edit history:
        # an ABA edit is invisible, and Git may skip a ref concurrently changed
        # to this exact validated new tip as already up to date. Cross-repository
        # source changes after the final check are also not transactional.
        git.run(bare, "push", "--atomic", *leases, policy.fork_url, *specs)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "sync"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--repo", required=True)
        cmd.add_argument("--output", required=True)
        if name == "prepare":
            cmd.add_argument("--local-stack", action="store_true")
        else:
            cmd.add_argument("--publish", action="store_true")
    cmd = commands.add_parser("validate")
    cmd.add_argument("--candidate", required=True)
    cmd = commands.add_parser("publish")
    cmd.add_argument("--candidate", required=True)
    cmd.add_argument("--repo", required=True)
    cmd.add_argument("--expected-manifest-sha")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.repo, args.output, local_stack=args.local_stack)
        elif args.command == "validate":
            result = validate(args.candidate)
        elif args.command == "publish":
            result = publish(args.candidate, args.repo, expected_manifest_sha=args.expected_manifest_sha)
        else:
            result = prepare(args.repo, args.output)
            if result["status"] != "noop":
                validate(args.output)
                if args.publish:
                    publish(args.output, args.repo)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (StackError, OSError, ValueError) as exc:
        print(f"stack-sync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
