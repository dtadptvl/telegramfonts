# TelegramFonts Capacity Model & A23 Dimensioning Guide

This document presents the mathematical capacity model, arrival rate formulas, and conservative consumer dimensioning for TelegramFonts under the target workloads of **500 downloads/day** and **1,000 downloads/day**.

---

## 1. Workload Specifications & Targets

- **Daily Target 1**: 500 orders/day = `0.005787` jobs/sec (1 job every `172.8` seconds).
- **Daily Target 2**: 1,000 orders/day = `0.011574` jobs/sec (1 job every `86.4` seconds).
- **Workload Shape**: 2–4 font styles per family, 3 distribution formats per style (`TTF`, `OTF`, `WOFF2`) -> 6 to 12 compiled font binaries packaged in a deterministic compressed `.ZIP` archive.
- **Maximum Steady-State Compute Utilization**: $\le 60\%$ (`0.60`) per A23 consumer node.
  - *Headroom Rationale*: 40% compute buffer absorbs network polling latency, D1 lease heartbeat round-trips, R2 upload streaming, and Poisson burst arrivals during peak hours without queue backlog accumulation.

---

## 2. Mathematical Dimensioning Formula

Given:
- $\lambda$: Average arrival rate in jobs per second ($\lambda = \frac{\text{Jobs/Day}}{86,400}$).
- $T_{p95}$: 95th-percentile end-to-end service latency per job in seconds (including acquisition, glyph contour polygonization, FontTools compilation, and ZIP packaging).
- $U_{max}$: Maximum target compute utilization ($U_{max} = 0.60$).

The minimum number of concurrent A23 consumer nodes $N$ required is:
$$N = \max\left(1, \left\lceil \frac{\lambda \times T_{p95}}{U_{max}} \right\rceil\right)$$

The maximum sustainable daily capacity of a single consumer node $C_{single}$ at $60\%$ utilization is:
$$C_{single} = \left\lfloor \frac{86,400 \times U_{max}}{T_{p95}} \right\rfloor$$

---

## 3. Representative Benchmark Estimates

Based on reproducible benchmark runs (`python agent/src/benchmark.py --samples 10`):

| Environment | $T_{p50}$ (Median) | $T_{p95}$ Latency | $N$ (500 jobs/day) | $N$ (1,000 jobs/day) | Single Node Capacity ($\le 60\%$ util) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Dev Host (AMD64 x86_64)** | ~1.75 s | ~1.88 s | **1 Node** (1.09% util) | **1 Node** (2.18% util) | ~27,500 jobs/day |
| **Simulated Low-End ARM64** *(Est. 5x slower)* | ~8.75 s | ~9.40 s | **1 Node** (5.44% util) | **1 Node** (10.88% util) | ~5,500 jobs/day |
| **Extreme Throttle** *(Est. 25x slower)* | ~43.75 s | ~47.00 s | **1 Node** (27.20% util) | **1 Node** (54.40% util) | ~1,100 jobs/day |

---

## 4. Policy on Capacity Proofs

> [!IMPORTANT]
> **Production Capacity Proof Policy**: Per Issue #16 policy, no development machine or GitHub Actions CI result may be labeled as production capacity proof. Formal verification of real-world A23 performance requires executing `python agent/src/benchmark.py` directly on the physical Samsung Galaxy A23 (Snapdragon 680 / ARM Cortex-A73) under Termux / Linux deploy.
