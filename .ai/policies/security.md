# Security and Secret Policy

Load whenever credentials, tokens, private keys, secret stores, authentication material, security incidents, or suspected exposure are materially involved.

## Secret handling

Never expose secrets in:
- source or tests;
- shell command arguments, shell output, or captured command/report output when secure injection/reference is available;
- Git/GitHub comments;
- `.prime/` journal/task results;
- chat/logs;
- screenshots/artifacts when a secure reference can be used.

Do not dump complete env/secret files for diagnosis.
Use secure injection/reference mechanisms; never pass plaintext secrets directly in command arguments when a safer mechanism exists.

## Suspected exposure

Treat suspected exposed credentials as compromised until containment/rotation/remediation evidence establishes otherwise.

```text
STOP relevant propagation
-> preserve non-secret evidence
-> mark security blocker
-> contain exposure
-> obtain required Human/Prime authorization for remediation
-> verify remediation/rotation where applicable
```

Security containment takes priority over feature completion.

## Boundary rule

Never weaken authentication/authorization/validation to make tests pass.
Any material security trade-off or changed trust boundary requires Prime decision/recontract.
