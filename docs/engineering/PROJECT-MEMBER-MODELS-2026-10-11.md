# Project member model settings

Project settings → Members → Project model stores `model`, `provider`, and
`reasoning_effort` by project and stable Bot ID in `group.db` settings under
`member_model:<bot_id>`. The separate Global configuration action retains the
existing host configuration editor. Project model saves never write host files.

Administrator GET/PUT `/projects/{project}/members/{profile}/model` verifies
project membership and runtime, validates fields, and uses a revision hash with
an immediate SQLite transaction to reject stale saves. Empty values inherit host
defaults. Provider names must already be configured in the member's environment.

Every chat/work wake reads these settings. Codex receives `--model` and TOML
`-c` overrides before the optional resume subcommand. Hermes receives `--model`,
`--provider`, and `--reasoning`. Changes affect subsequent wakes; in-flight runs
retain their arguments and existing conversation bindings remain intact.

Hermes restores a historical model on resume unless `--model` is explicit.
For a saved project setting with an empty model, the engine therefore reads the
current physical profile's default model and passes it explicitly. This also
handles clearing an override and provider-only overrides. Supported host model
forms include strings, default/model/name aliases and nested model mappings.
A missing default fails the wake with a diagnostic; configure a project model
or host default. Projects that have never saved model settings retain the
existing launch behavior.

Validation uses isolated SQLite/config directories, mocked subprocess launches,
and real Chrome with synthetic HTTP responses. Production models/sessions are
not used for tests. Restart the engine manually and refresh the page to enable
the new API and launch behavior.

The optional native probe `python -B scripts/verify_project_model_resume.py`
uses a disposable home and loopback fake Responses provider to inspect the
requested model on creation and resume. Record its results in reports/tests/.
