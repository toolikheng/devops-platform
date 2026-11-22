# ADR 018: One fake-CLI module for the script tests; the cost check keeps its own

## Status

Accepted

## Context

Most scripts in this repo drive other programs: `kubectl`, `terraform`, `aws`, `helm`, `curl`, `docker`, `ansible-playbook`. Their tests put fake programs first on `PATH`, run the real script or Make target, and check what was called. By Phase C5 there were three sets of fakes:

- `FakeAws` in `tests/test_cost_check.py` (C0): one small `aws` fake that answers from files named `service.operation[@region]`.
- `Fakes` in `tests/test_cloud_targets.py` (C2/C3): a bash fake for `terraform`, `aws`, `curl` and `ansible-playbook`, logging `tool|cwd|profile|key|args`.
- `Fakes` in `tests/test_policypal_scripts.py` (C4/C5): a Python fake for seven tools, logging JSON lines, with more than twenty file-name rules.

The two `Fakes` faked the same tools with different behaviour (`terraform output -raw X` printed the instance ID for any X in one and `tf-X` in the other), so no test could cover a target that crossed both. Tests often wrote the harness's files directly (`tf-state`, `applied`, `curl_stcore_health`), and the External Secrets Operator and state-check tests sat in the PolicyPal file only because its fake answered `helm`, `ssm` and `state pull`.

## Decision

- **`tests/fakecli.py` is the one fake-CLI module** for the script and Make-target tests. One Python fake program serves every tool and logs each call as a JSON line (args as a list, kubectl's namespace, `KUBECONFIG`, `AWS_PROFILE`, whether Terraform got the SSH key, working directory, stdin). Tests set up what the scripts find and read what they did through `Fakes` methods, never through its files. Any call can be made to fail (`fail("kubectl", "rollout", "undo", ...)`), and a real command can be logged on its way through (`spy("grep")`, used to prove a secret value is never a `grep` argument).
- **Test files follow the script they test:** `test_policypal_scripts.py` (Secrets, smoke test, deploy), `test_cloud_targets.py`, `test_eso_scripts.py`, `test_check_state_secrets.py`.
- **`FakeAws` stays separate.** Its 19-line fake covers 39 cost-check cases and needs no other tool. Moving it would tie the C0 suite to a log format built for other scripts and gain nothing. Its `service.operation` answers are the model the shared fake copies for `aws`.

## Consequences

- One fake per tool: a behaviour the scripts rely on (kubectl's `--for=create` with one resource, curl's `-f`) is modelled in one place.
- The merge kept all 289 tests with the same IDs. Of 17 deliberate bugs in the scripts and Makefile, the same 16 were caught before and after it, each first by the same test. The one that survives both drops curl's `-f` from the smoke test's health check, which changes nothing observable: the smoke test also reads the status in the body.
- Failure paths are easier to reach: any call can fail. A rollback bug had hidden behind a fake that couldn't fail a `rollout undo` (a failed undo of the API stopped the UI's rollback); it was fixed just before the merge.
- The fake program is now the biggest piece of test code (about 280 lines with its `Fakes` class), and every script test depends on it. A change to how it answers can break tests in four files.
- Revisit `FakeAws` only if the cost check starts calling a tool other than `aws`.
