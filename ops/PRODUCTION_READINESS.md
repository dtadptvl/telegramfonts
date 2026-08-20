# TelegramFonts Production Readiness Checklist

This document tracks release candidate validation status across the TelegramFonts repository and outlines the remaining operational tasks required for full production launch.

---

## 1. Release Candidate Status Matrix

| Component / Requirement | Category | Status | Verification Evidence |
| :--- | :--- | :--- | :--- |
| **Control Plane Typecheck** | Build / Typing | `REPO_PASS` | `npm run typecheck` passes with zero errors |
| **Edge Automated Test Suite** | Automated Tests | `REPO_PASS` | 139 tests across 11 test suites passing (Vitest / Cloudflare Workers Pool) |
| **Agent Test Suite** | Automated Tests | `REPO_PASS` | 48 unit & integration tests passing (pytest) |
| **Release Preflight Command** | Config Validation | `REPO_PASS` | `npm run preflight` executes deterministically, validating bindings and parameters |
| **Capacity Benchmark Harness** | Performance Tooling| `REPO_PASS` | `python agent/src/benchmark.py` generates JSON reports and latency models |
| **Multi-Consumer Fencing Proof**| Concurrency Proof | `REPO_PASS` | `edge/test/multi-consumer.spec.ts` proves singular fulfillment across races & retries |
| **Operator Launch Runbook** | Operational Docs | `REPO_PASS` | `ops/RUNBOOK.md` defines exact step-by-step launch, health checks, and rollback |
| **Observability Specifications** | Observability | `REPO_PASS` | `ops/OBSERVABILITY.md` defines sanitized schemas and log tailing commands |
| **Security Architecture Guide** | Security Matrix | `REPO_PASS` | `ops/SECURITY.md` defines token isolation and secret rotation steps |
| **Capacity Model Guide** | Capacity Planning | `REPO_PASS` | `ops/CAPACITY_MODEL.md` details 500 & 1000 jobs/day formulas |
| **Remote D1 Provisioning** | Cloudflare Infra | `WAIT_HUMAN_RUNTIME` | Human must execute `wrangler d1 create` and apply remote migrations |
| **Remote Queue Creation** | Cloudflare Infra | `WAIT_HUMAN_RUNTIME` | Human must execute `wrangler queues create` and enable HTTP pull consumer |
| **Private R2 Bucket Creation** | Cloudflare Infra | `WAIT_HUMAN_RUNTIME` | Human must execute `wrangler r2 bucket create` |
| **Production Secrets Injection**| Security Config | `WAIT_HUMAN_RUNTIME` | Human must execute `wrangler secret put` for Telegram, SePay, Node, and HMAC secrets |
| **Worker Production Deploy** | Deployment | `WAIT_HUMAN_RUNTIME` | Human must execute `wrangler deploy` |
| **Telegram Webhook Binding** | Integration | `WAIT_HUMAN_RUNTIME` | Human must register HTTPS webhook with Telegram Bot API |
| **Physical A23 Benchmark** | Hardware Proof | `WAIT_HUMAN_RUNTIME` | Human must run benchmark CLI on physical Samsung Galaxy A23 device |

---

## 2. REPO_PASS Summary

All repository code, migrations (0001–0005), route handlers, D1 transactional batches, R2 streaming endpoints, HMAC signed downloads, outbox dispatchers, A23 worker daemon, benchmark tooling, and test suites are fully implemented and green in CI.

---

## 3. WAIT_HUMAN_RUNTIME Next Steps

To transition the project from Release Candidate to Live Production:
1. Follow `ops/RUNBOOK.md` Steps 2–6 to provision Cloudflare resources and deploy the Edge Worker.
2. Follow `ops/RUNBOOK.md` Step 7 to configure the A23 Android/ARM64 compute worker.
3. Execute `python agent/src/benchmark.py --samples 20 --json-out ops/a23_device_benchmark.json` on the physical A23 device to capture the authoritative hardware capacity proof.
