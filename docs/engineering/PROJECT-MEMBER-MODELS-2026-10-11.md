# Project member model settings

Project details → Members → Edit profile → Model and provider stores `model`, `provider`, and
`reasoning_effort` by project and stable Bot ID in `group.db` settings under
`member_model:<bot_id>`. Selecting **Apply globally** writes the fields to
the member's profile and clears their overrides in the current project.

Administrator GET/PUT `/projects/{project}/members/{profile}/model` verifies
project membership and runtime, validates fields, and uses a revision hash with
an immediate SQLite transaction to reject stale saves. Empty values inherit host
defaults. Provider names must already be configured in the member's environment.

Every chat/work wake reads these settings. Codex receives `--model` and TOML
`-c` overrides before the optional resume subcommand. Hermes receives `--model`,
`--provider`, and `--reasoning`. Changes affect subsequent wakes; in-flight runs
retain their arguments and existing conversation bindings remain intact.

For a resumed Hermes gateway session, the engine sends the selected model and
provider through `session.mutate` before submitting the prompt. It caches the
selection after a committed model receipt. A confirmation request or a model
resolution failure leaves the project selection pending and the turn proceeds
on the session's current model. The engine sends one project message addressed
to the user for consecutive failures of the same target in the same session.
The next wake retries. A successful switch clears the notification marker.
Legacy cache entries are rechecked on first wake after upgrade.

First registration of a local Hermes Bot sets
`model.switch_context_confirm_tokens: 0` and `display.busy_input_mode: steer`
in its bound profile, including an existing profile selected during registration.
Re-enrolling a registered Bot preserves the profile's settings. The context
threshold controls the long-context warning. Cost and data-policy warnings
retain their own confirmation rules.

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
requested Codex model on creation and resume. The native Hermes probe captures
the first outgoing model request and counts user notifications across four wakes
of the same session. See the [model switch test commands](../../scripts/TEST_INDEX.md#hermes-项目成员换模).
