# Maintaining the gptel stack

## Branches and trust boundary

The application history is linear:

```text
karthink/gptel master -> pr-1338-layer (one squashed PR #1338 commit)
                     -> local-stack (individual personal commits)
```

`local-fixes-effort`, `local-fixes`, and their remote counterparts are historical
sources, not synchronization targets. Do not rewrite them. The fork's default
branch remains `master`.

Automation lives separately on `stack-sync-setup`, based on the fork's `master`.
Only after separate approval is it installed on existing `master`.
`local-stack` intentionally receives no synchronization scripts or this guide.
Use a trusted control worktree for tools, independent of the application checkout:

```bash
# Run in the fork clone; choose unused worktree paths.
git worktree add ../gptel-control stack-sync-setup
CONTROL=$(cd ../gptel-control && pwd)
REPO=$(pwd)  # repository holding the application refs; not a candidate checkout
```

After activation, use reviewed automation from `master` in the control worktree
instead. Inspect control-code updates before running them; never run tools taken
from the untrusted candidate branch.

## Activation is a separate, approved operation

Nothing here authorizes a push or changes repository settings. Before activation:

1. Review the initial mapping below, tree-preservation evidence, scripts, tests,
   action pins, workflow, and validation results. Resolve any failing checks.
2. With explicit approval, publish the initial `pr-1338-layer` and `local-stack`
   branches to `FrauH0lle/gptel`. Do not publish newer synchronization candidates
   in place of the exact initial reconstruction without review.
3. With separate approval, install the reviewed automation from `stack-sync-setup`
   on the existing default `master`. Do not change the default branch or merge
   tooling into `local-stack`.
4. Enable Actions on the fork, including scheduled workflows. Check that Actions
   policy permits the pinned actions and the workflow's `contents: write` job.
   Branch rules for **both** destination branches must allow the workflow actor's
   leased history rewrites; tag rules must allow create-only backup tags. Keep
   unrelated protection rules unchanged. An atomic rejection is a stop, not a
   reason to bypass protections or split the push.
5. Dispatch **Synchronize gptel stack** on `master`; inspect all three jobs,
   summaries, artifacts, remote refs, and backup tags before relying on scheduling.
   A no-op skips application validation; arrange a reviewed update run to verify
   the full validation/publication path.

The workflow runs daily at **03:37 UTC** and manually via `workflow_dispatch`,
only for `FrauH0lle/gptel` on default `master`. Runs are serialized without
cancelling an active run. Scheduled runs can be delayed or dropped under GitHub
load; public-repository schedules can be disabled after 60 days without repository
activity. Check Actions when updates appear stale. No PAT is planned: publication
uses the job-scoped `GITHUB_TOKEN`. Pushes made with it do **not** start ordinary
push-triggered Actions, so validation happens before publication in this workflow.

## Prepare, validate, and publish

Requirements: Python 3, Git, and Emacs **31.1** on `PATH` for validation; network
access to GitHub and stable GNU/NonGNU ELPA over HTTPS (not MELPA).
The trusted validator provisions declared packages
plus the test-only `markdown-mode` prerequisite in isolation and fetches the exact recorded test-suite gitlink from
`karthink/gptel-test`. It compiles candidate Lisp, runs the complete recorded ERT
suite with nonzero discovery, and records versions. Candidate code runs only
without publication credentials. Do not export `GH_TOKEN` or `GITHUB_TOKEN` into
validation; the runner rejects them.

Use a new, nonexistent output directory for each preparation:

```bash
CANDIDATE="$HOME/stack-candidates/$(date -u +%Y%m%dT%H%M%SZ)"
python3 "$CONTROL/scripts/stack-sync.py" prepare --repo "$REPO" --output "$CANDIDATE"
# Inspect manifest.json: exact old/new tips, source pins, status, source_mode.
python3 "$CONTROL/scripts/stack-sync.py" validate --candidate "$CANDIDATE"
```

Before initial publication, the two new branches exist only locally. Use
`prepare ... --local-stack` for that preactivation dry-run. It still fetches/pins
upstream and PR state, but reads the initial destination tips locally and marks
`source_mode: local`. **Local-source candidates can never be published**, even
after validation. After initial branches are published, prepare again without
`--local-stack` to use remote snapshots.

Preparation does not change the current checkout or include unpublished commits
implicitly. It validates layer metadata and linear personal history even for a
no-op. A closed/merged PR, conflict, unexpectedly empty replay, malformed history,
missing branch, or network error stops the process for manual review.

Keep `manifest.json` and `candidate.bundle` unchanged. Successful validation adds
`validation.json`, binding the report to both original file digests and the exact
candidate stack. Raw runner diagnostics, including failures, are saved separately
as `validation-report.json`; these never authorize publication. Inspect the report
and command exit status; a report alone is
not a substitute for a successful trusted validation run. Compare an original
snapshot validation separately when diagnosing baseline failures; never waive
candidate failures to publish.

For an explicitly approved publication of a **remote-source** candidate:

```bash
MANIFEST_SHA=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$CANDIDATE/manifest.json")
# Supply a permitted credential to this invocation only, never to validation.
python3 "$CONTROL/scripts/stack-sync.py" publish --repo "$REPO" \
  --candidate "$CANDIDATE" --expected-manifest-sha "$MANIFEST_SHA"
```

The production CLI fixes the fork, upstream, PR, and two destination refs; it has
no arbitrary push destination option. It rechecks source snapshots and uses one
atomic push with explicit expected-SHA leases for both branches and create-only
backup tags. A concurrent destination change to a different value rejects the
entire push. Git can treat a ref already at the exact validated new tip as
up-to-date without checking its old-tip lease; changed-and-restored (ABA) edits
are also undetectable. The desired final state is still the validated candidate,
but not every intervening edit is detectable. A small cross-repository race also
remains after the last source check: GitHub cannot transact upstream movement
and the fork's push together. Rerun preparation after a stale-source or lease
rejection; do not relax the checks.

`sync --repo "$REPO" --output "$CANDIDATE"` is the local convenience command;
it does not publish unless `--publish` is explicitly supplied. CI deliberately
uses separate fresh prepare/read-only, validate/read-only, and publish/write jobs
at the same trusted `github.sha`. It retrieves the original preparation artifact
by immutable ID, retrieves only the separate validation report by its ID, checks
the expected manifest digest, and requires validation-job success. The writer
never checks out or executes candidate code or shares a validation cache.
No-op summaries explicitly mean **no fresh application tests**.

Local environment scrubbing is **not a sandbox**. Hostile Lisp can access other
same-user processes and files, including credentials outside its own environment.
Run untrusted candidates only in disposable credential-free environments; do not
use authenticated local `sync --publish` as a security boundary. The three fresh
CI jobs, not process environment filtering, separate testing from writer credentials.

## Working on personal changes safely

Use a topic branch rather than committing directly onto a CI-rewritten branch:

```bash
git fetch origin
git switch -c my-topic origin/local-stack
BASE=$(git rev-parse origin/local-stack)  # retain this old base for later replay
# Edit, test, and commit on my-topic.
```

After CI rewrites the remote stack, do not blindly `git pull`, merge, or reset a
checkout with local work. Save uncommitted changes first. While still on your
topic, preserve a local backup and replay only its unpublished commits:

```bash
git branch my-topic-before-stack-update
git fetch origin
git rebase --onto origin/local-stack "$BASE" my-topic
# Resolve conflicts deliberately; use git rebase --abort if unsure.
# Review/test the result, then remember the new origin/local-stack as BASE.
```

Record the base before fetching away your only reference to it. If commits are
already shared, coordinate their rewrite rather than applying this unpublished
topic recipe blindly. To add reviewed personal commits to the maintained remote
stack, start from its current tip, keep history linear, validate, and use an
explicit publication approval and expected-tip lease. Do not race the scheduled
writer or insert merges. CI maintains pushed personal commits only.

## Conflicts and recovery

On preparation or validation failure, published refs remain unchanged. Read the
failing step, manifest (if available), and validation report. Resolve integration
changes in a disposable/topic checkout; preserve intentional empty commits and
review any unexpectedly empty replay. Do not edit candidate artifacts to make a
failed run pass. Reprepare and revalidate from reviewed branch state. If the PR
closes or merges, review a new stack policy manually rather than silently dropping
the layer.

Successful updates retain the previous two tips as annotated backup tags:
`stack-backup/<run-id>/pr-1338-layer` and `stack-backup/<run-id>/local-stack`.
The run ID is the original manifest's SHA256 digest; each tag records that digest
and a nonce so create-only leases cannot be bypassed as an "up-to-date" push.
Download/retain the run's
manifest and validation report; GitHub artifacts expire after 14 days, while tags
remain until explicitly removed.

Recovery is an approved remote rewrite, not an automatic rollback. Pause scheduled
publication and coordinate with other writers first. Fetch the chosen backup pair,
inspect and validate its relationship/tree, and capture the **current remote tips**:

```bash
git fetch origin '+refs/heads/pr-1338-layer:refs/remotes/origin/pr-1338-layer' \
  '+refs/heads/local-stack:refs/remotes/origin/local-stack' \
  'refs/tags/stack-backup/*:refs/tags/stack-backup/*'
RUN_ID=<chosen-manifest-sha256>
OLD_LAYER=$(git rev-parse origin/pr-1338-layer)
OLD_STACK=$(git rev-parse origin/local-stack)
RESTORE_LAYER=$(git rev-parse "refs/tags/stack-backup/$RUN_ID/pr-1338-layer^{commit}")
RESTORE_STACK=$(git rev-parse "refs/tags/stack-backup/$RUN_ID/local-stack^{commit}")
# Review the selected tags before executing this separately approved push.
git push --atomic \
  --force-with-lease="refs/heads/pr-1338-layer:$OLD_LAYER" \
  --force-with-lease="refs/heads/local-stack:$OLD_STACK" origin \
  "$RESTORE_LAYER:refs/heads/pr-1338-layer" \
  "$RESTORE_STACK:refs/heads/local-stack"
```

Never restore just one branch, use bare `--force`, or silently retry without
`--atomic`. A rejected lease requires inspecting the concurrent change. Retain
the pre-recovery tips separately before rollback. Once verified, re-enable the
schedule; if upstream/PR has advanced, a later sync may reconstruct a newer stack.

## Initial migration record

The reconstruction is pinned to original
`084a70cbe6d6f67be56d265a48c0909325bf1fcb`, upstream
`e7cebe4621533a036840d47c9741682bb687d808`, and PR head
`464f6f5d896493ad03f267d91a6210838b5a0397`.
Initial `pr-1338-layer` is `adbc1b0ffffb7db2eea4566f7799e9027ba762ee`,
with exactly one parent: that upstream commit. Initial `local-stack` is
`75079ccdcf17b21d929970ae6f7236236538a693`. Both the original and reconstructed
snapshots have tree `a0a633ec0c5151111bfba9ce00662c31447ef992`; the original is
also retained by local tag `stack-migration/20261025-original`.

| Original contribution | Reconstructed commit | Treatment |
| --- | --- | --- |
| `965e910` | `0d95331c3f873a430cbecf992b1d9ecaed0cb62d` | Prompt-buffer cleanup |
| `c80fa6b` | `f4705d390c9ad20226d79b600ff28c41c23b2352` | Reasoning block insertion |
| `df1e6d0` | `8b17decd492a9032a92246bab32aa31645b67fea` | Preserve chunks |
| `f9a12df` | `8195c893c5958950da30e734bcd92260e06ee07d` | Replay response items, adapted around the reverted stream-closing change |
| `1dde559` | `46b4b453b583ebc1ef29b8e9bf30db76a406feb0` | Temperature default, retaining the PR's nonnil guard |
| Merge `4276317` residual | `75079ccdcf17b21d929970ae6f7236236538a693` | Provider-string integration adjustment with original provenance |
| `fa8b524`, `562b3da` | Squashed PR layer | Patch IDs match PR commits `f84862d`, `ecf69f4`, respectively; not personal replay |
| `4573f93` | Squashed PR layer | Substantive reasoning-effort effects already covered by PR |
| `7542fe4` and revert `1f2b08d` | Omitted pair | Not byte-inverse after intervening edits; adapted replay retains surviving behavior |

Complete tracked-tree equality, **including the test submodule gitlink**, was
verified before any new synchronization. The application branches contain no new
tooling. Reproduce the structural checks against these initial refs:

```bash
git diff --exit-code 084a70c 75079ccdcf17b21d929970ae6f7236236538a693
git merge-base --is-ancestor adbc1b0ffffb7db2eea4566f7799e9027ba762ee 75079ccdcf17b21d929970ae6f7236236538a693
git rev-list --count e7cebe4621533a036840d47c9741682bb687d808..adbc1b0ffffb7db2eea4566f7799e9027ba762ee
git rev-list --merges e7cebe4621533a036840d47c9741682bb687d808..75079ccdcf17b21d929970ae6f7236236538a693
```

Expect count `1` and no merges. The exact equality accounts for all surviving
effects, including the reasoning-effort change, adapted fix/revert handling, and
merge-resolution residual; author names and subjects were not used as classifiers.

### Local verification on 2026-10-25

The real `prepare --local-stack` path fetched the live public sources and produced
a verified no-op: upstream and PR still matched the pinned revisions above. This
was preparation evidence, not an application-test pass. No remote publication or
workflow activation was performed.

The original snapshot and the exact prepared candidate were validated separately
with Emacs 31.1 and test gitlink
`0d95a1b8ccf08b5da4fa078f7b7b1b34f937785e`:

| Scenario | Check / metric | Result |
| --- | --- | --- |
| Tooling | Standard-library regression tests passed | 57 tests |
| Workflow | actionlint 1.7.12 | Pass |
| Original and candidate, each | Successful compilation | 18 Lisp files |
| Original and candidate, each | ERT completed | 171 tests |
| Original and candidate, each | ERT unexpected failures | 9 tests |
| Original and candidate, each | ERT skipped | 0 tests |

Provisioned package versions were `transient` 0.13.8, `compat` 31.1.0.0,
`cond-let` 1.1.5, `llama` 1.0.6, and test prerequisite `markdown-mode` 2.8.
The identical failing tests in both runs were:

- `gptel-test-persist-restore-bounds`
- `gptel-test-prompt-anthropic-md`
- `gptel-test-prompt-gemini-md`
- `gptel-test-prompt-ollama-md`
- `gptel-test-prompt-openai-md`
- `gptel-test-prompt-openai-md-before-max`
- `gptel-test-stream-parsing-deepseek-01a`
- `gptel-test-stream-parsing-openrouter-02a`
- `gptel-test-stream-parsing-openrouter-02b`

**Application validation failed; initial publication and activation remain blocked.**
Baseline reproduction does not waive candidate failures. Resolve these failures
in separately reviewed application work and revalidate before activation; do not
weaken tests or alter the tree-preserved migration to manufacture a pass.

## Checking automation changes

```bash
cd "$CONTROL"
python3 -m unittest discover -s scripts/tests -p 'test_*.py' -v
actionlint .github/workflows/sync-stack.yml
```

Action pins were checked against primary upstream release/tag/commit APIs on
2026-10-25: `actions/checkout` v7.0.1, `actions/upload-artifact` v7.0.2,
`actions/download-artifact` v8.0.2, and `purcell/setup-emacs` commit
`34c6ded44899fd1bf74d2889558befd1750e61a7` (adds Emacs 31.1).
The workflow records full immutable SHAs. Review upstream source and input/runtime
changes before updating them. The pinned Emacs action resolves its Nix toolchain
externally, and validation provisions packages from GNU ELPA: action pins do not
make all external dependencies immutable. Transitive dependencies can come from
stable NonGNU ELPA. Use recorded versions when diagnosing
reproducibility. Actual GitHub token, branch-rule, and schedule behavior must be
checked during approved activation; local tests cannot prove those settings.
