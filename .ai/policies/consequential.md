# Consequential Action Policy

Load before material destructive, externally mutating, difficult-to-reverse, single-shot, or high-impact action.

Normal bounded repository edits/commits/PR operations are not automatically Human-reserved. Repository merges are still Prime-authorized, identity-bound, and single-attempt per `review.md`. Production/destructive external actions are Human-reserved by default unless explicit durable project policy grants Prime authority.

## Exact authorization binding

A worker may execute a consequential action only when its contract contains current explicit binding sufficient for that action, e.g.:

```yaml
authorization:
  owner: prime | human
  action: <exact action>
  target: <exact target>
  identity: <commit/artifact/deployment binding when relevant>
  mutation: <allowed surface/count>
  max_attempts: 1
  stop: [<explicit conditions>]
  auth_ref: <durable reference>
```

Generic continuation, prior similar approval, DONE, green tests, positive prose, or implied permission is not executable authority. Human approval becomes executable only after Prime binds it into the current authorization.

Authorization never silently permits another target, retry, repair-forward, alternate transport, adjacent mutation, unrelated source/config/package changes, rollback, or second destructive step.

## Attempt accounting

One attempted single-shot action consumes allowance unless authoritative evidence proves `NOT_ATTEMPTED`. Timeout/ambiguous result does not restore it. Before any repeat, reconcile authoritative state and obtain new authority when required.

Report authorization state when material:

```text
CONSUMED | UNCONSUMED | AMBIGUOUS
```

Do not claim `UNCONSUMED` without evidence proving no attempt occurred.

## Pre-mutation gate

Immediately before acting, verify authorization, exact target/identity, allowed mutation/count, stop conditions, and required security/production policies. If materially ambiguous: STOP and return to Prime.

## Rollback discipline

When rollback could materially change how a consequential action is executed or accepted, make it explicit in the contract. Keep it bounded to a trigger, action/target, and required evidence/authorization. A rollback plan is **not** rollback authority. If rollback is itself consequential, it requires its own current executable authorization unless the existing authorization explicitly includes that exact rollback.
