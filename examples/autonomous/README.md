# Real Codex CPU smoke

From the repository root:

```sh
python3 -m rsi_service serve --config examples/autonomous/config.json
python3 -m rsi_service status --config examples/autonomous/config.json
```

This uses your configured Codex CLI for all six model roles. The task itself is
Python standard-library work: fix a loader observer, run four callback protocol
cases, and pass an independent fixed operator check. No NPU, remote service,
package installation or non-model network task is required.

The seed session is synthetic. The old skill recipe uses a narrow callback
signature; the allowed candidate change is `skill/notes.md`. Each actual run
starts in its own copy of `target/`. Original files remain unchanged.

Limits are one cycle, two validation runs, twelve model-role jobs and thirty
minutes overall. The default state directory is `/tmp/rsi-autonomous-cpu-smoke-v1`.
Reuse it only to resume the same immutable experiment. For a fresh experiment,
copy this example directory and set a new state directory before first launch.

Successful loop execution does not guarantee promotion: an already effective
baseline or ineffective candidate must be rejected. Read
[the autonomous-mode guide](../../docs/AUTONOMOUS.md) for configuration, evidence,
comparison policy, long-session analysis, stop/cancel/recover and limitations.

One actual Codex smoke completed with eight model-role jobs and two executions:
both versions passed all four fixed cases, while observed failed tool calls fell
from one to zero. The controller promoted the candidate and recorded a supported
hypothesis. See [the recorded result](validation-report.json). This was one CPU
pair using full-payload analysis; it is not a repeatability or Day0/NPU claim.
