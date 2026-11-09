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

Hermes restores historical session policy on resume unless the desired fields
are explicit. On every registered Hermes chat/work wake, the engine resolves
each empty project field from the current physical profile's `config.yaml`,
including projects with no saved override row. It passes the effective model,
provider and reasoning effort explicitly when configured. Project overrides
win field by field, YAML `false` reasoning maps to `none`, and clearing an
override resumes host inheritance. This matches the values displayed by the
unified member editor without creating a project setting or changing host files.
Supported host model forms include strings, default/model/name aliases and
nested model mappings. A missing effective model fails the wake with a
diagnostic; configure a project model or host default. Existing bindings and
conversation history remain intact; in-flight runs retain their policy.

The distinction between configured defaults and retained native session policy,
the effective launch arguments and validation boundaries are described in
[Hermes model inheritance](../design/2026-11-09-hermes-model-inheritance.md).

Validation uses isolated SQLite/config directories, mocked subprocess launches,
and real Chrome with synthetic HTTP responses. Production models/sessions are
not used for tests. Restart the engine manually and refresh the page to enable
the new API and launch behavior.

The optional native Codex probe `python -B scripts/verify_project_model_resume.py`
uses a disposable home and loopback fake Responses provider to inspect the
requested Codex model on creation and resume. Hermes gateway mutation payloads
are covered by the isolated project-model and session-policy regressions.
Record native probe results in reports/tests/.
