# Codex independent profile homes

Subprofiles live in `$CODEX_HOME/profiles/<name>/`. Each directory is an independent Codex home with its own `config.toml`, `AGENTS.md`, `memories/`, and `skills/`. Authentication must be provisioned for that home before model execution.

Wizard-created Codex homes always start with `approval_policy = "on-request"`, `approvals_reviewer = "auto_review"`, and `sandbox_mode = "workspace-write"`, including when copying an existing configuration source. Their writable roots come from the selected project directories. Windows homes receive an explicit sandbox implementation. See [creation policy](../engineering/PROFILE-CREATE.md). Existing-Bot enrollment preserves approval and sandbox settings, shows advisory notices when either needs attention, and still grants the assigned project directories.

## File authentication synchronization

New Codex profiles inherit `auth.json` from the engine's primary Codex home (`CODEX_HOME`, default `~/.codex`). Selecting a configuration source affects model and runtime configuration; authentication comes from the primary home. A copied file uses `cli_auth_credentials_store = "file"`.

Before launching a child, the engine checks the access token's JWT `exp`. A valid local token stays unchanged. Missing credentials or an expired access token receive the primary home's currently usable file credentials through an atomic replacement. If the child has a known expired token and the primary has no usable file credentials, launch returns a message asking the administrator to restore primary-account authentication. Unknown token formats and keyring authentication continue through native Codex handling.

A runtime authentication error (401, `token_revoked`, or `refresh_token_invalidated`) triggers at most one additional check per execution. Different usable primary credentials can replace the child file even before its recorded expiry. Tool output and model text do not trigger this check. The engine leaves execution retries to Codex and does not replay business operations; the updated file is available for native credential reload or the next launch.

Synchronization is local file I/O. Token renewal remains managed by Codex. The primary file is never overwritten by synchronization. A different recorded account ID blocks copying, malformed credentials produce sanitized errors, and changes detected while staging a copy preserve the newer file. Per-process locking serializes engine copies; native refresh behavior remains unchanged.

Existing registered profiles receive the same check on their next launch. Apply the engine update with a manual restart and refresh the frontend. Regression coverage uses temporary homes, synthetic tokens, mocked subprocesses, and Chrome with synthetic APIs.

## Discovery and launch

Discovery records the child directory itself as `config_root`. The driver launches it with `CODEX_HOME=<child directory>` and no named-config selector. Discovery remains read-only; project membership requires explicit enrollment. Existing root-level named configuration files and independent homes remain discoverable.

The `profiles` container is never a candidate. Child homes require a recognized Codex artifact; empty directories are ignored. Moving a registered home changes its identity key and requires a deliberate migration preserving its Bot ID.

Validation: `python -B -m unittest scripts.test_discovery scripts.test_codex_driver_regressions -q`. Restart the engine manually after updating discovery code.

## Project directory access on enrollment (2026-10-16)

Joining a project persists that member's assigned directory as writable in its Codex configuration. Shared members receive the project root; worktree members receive their assigned worktree path and the main project's `reports/` directory when it lies outside that worktree. Requirements, designs and technical documentation are versioned in the assigned worktree; test reports are shared at the main project root. See [document storage](DOCUMENT-STORAGE.md). Existing-profile enrollment, profile creation with enrollment, project import and project initialization use `engine/codex_project_access.py`. Discovery, ordinary launches and session resumes do not write permission settings.

Explicit workspace creation, adoption, member directory-mode changes, and changes to a project's root/directory mode/worktree root also persist the selected directory grants before committing the database transaction. Configuration failures restore database state and previously written configuration bytes. A Git worktree already created before failure stays on disk for explicit adoption. Existing grants remain because they may be shared with another project. Enrollment uses the same administrator identity gate as other member management APIs; Bot identities receive HTTP 403. The existing password-disabled development identity behavior is retained pending the separate login design.

Named permission policies receive filesystem entries for the assigned directories. Built-in `:read-only` / `:workspace` policies are extended by a local named policy; an unset policy uses the CLI's read-only default as its base. Legacy `workspace-write` configurations receive these directories in `sandbox_workspace_write.writable_roots`. Existing model, authentication, approval/reviewer, network and unrelated filesystem settings remain intact. Explicit legacy read-only mode or an explicit deny on the target directory returns a conflict for the user to resolve. Full-access profiles already cover the directory and require no additional grant.

The selected `<profile>.config.toml` is used for named profiles, with inherited settings read from `config.toml`; legacy inline profile overrides are retained if a named file must be created. Independent homes use their own `config.toml`. Existing files are backed up under `.group-engine-backups` before atomic replacement. Enrollment failures roll back database changes and restore configuration bytes; repeated enrollment with the same directory performs no additional write. New homes contain their grant before publication. Removing membership does not revoke persisted grants in this change.

Activation requires a manual engine restart. Previously enrolled members are not swept or modified automatically; repeating `POST /bots/register` with the same project and Bot identity adds their missing directory grant without removing membership. The current add-member UI filters existing project members, so this repair uses the enrollment API. Existing Codex sessions may retain old permissions, so use `/new` after re-enrollment before continuing work. This change does not resume tasks automatically.

Validation uses temporary homes and databases: run `scripts.test_codex_project_access`, enrollment/profile creation/role tests, and the relevant import/setup/template regressions. See the [test index](../../scripts/TEST_INDEX.md). Store run results and native CLI evidence in the main project's `reports/` directory.
