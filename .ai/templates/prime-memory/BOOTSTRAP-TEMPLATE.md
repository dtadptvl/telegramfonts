# Optional Project Bootstrap

Use only when project/runtime setup or off-machine recovery needs durable project-specific configuration. Do not copy core governance here. Delete unused fields. Never store secrets.

```text
# Recovery boundary: local | off_machine
recovery_boundary: off_machine

# Required only for off_machine. Use an existing authorized non-deploy Git target.
recovery_remote: <git-remote-name>
recovery_ref: <dedicated-recovery-ref>
recovery_push_authorized: true
recovery_push_non_deploying: true
```

`local` is valid only when the project workspace/worktree is guaranteed to survive the failure modes that must be recovered from. If that guarantee is absent, use `off_machine` or explicitly accept that workspace loss is outside the recovery guarantee. Prime verifies actual Git remote/ref identity before each allowed checkpoint; this file is configuration, not cached sync truth.
