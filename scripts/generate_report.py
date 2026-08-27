from __future__ import annotations

import argparse
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from redis.exceptions import RedisError

from reliability_lab.cache import SharedRedisCache
from reliability_lab.config import load_config


def _format(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _met(actual: object, target: float, *, minimum: bool) -> str:
    if not isinstance(actual, (int, float)):
        return "N/A"
    passed = actual >= target if minimum else actual < target
    return "Yes" if passed else "No"


def _redis_evidence(redis_url: str) -> tuple[str, str, str]:
    prefix = "rl:report:"
    first: SharedRedisCache | None = None
    second: SharedRedisCache | None = None
    try:
        first = SharedRedisCache(redis_url, 300, 0.92, prefix=prefix)
        second = SharedRedisCache(redis_url, 300, 0.92, prefix=prefix)
        if not first.ping() or not second.ping():
            return "Redis unavailable", "Redis unavailable", "Not verified"
        first.flush()
        first.set("shared cache proof", "visible from instance two")
        shared_value, score = second.get("shared cache proof")
        keys = sorted(str(key) for key in first._redis.scan_iter(f"{prefix}*"))
        ttl = first._redis.ttl(keys[0]) if keys else -2
        privacy_query = "account balance for user 123"
        first.set(privacy_query, "must not be stored")
        privacy_value, _ = second.get(privacy_query)
        shared = f"instance_2_read={shared_value!r}, score={score:.2f}, ttl={ttl}s"
        key_output = "\n".join(keys) if keys else "(no keys)"
        privacy = "verified: sensitive query was not stored" if privacy_value is None else "failed"
        return shared, key_output, privacy
    except RedisError as exc:
        return (
            f"Redis evidence unavailable: {type(exc).__name__}",
            "Redis unavailable",
            "Not verified",
        )
    finally:
        if first is not None:
            with suppress(RedisError):
                first.flush()
                first.close()
        if second is not None:
            with suppress(RedisError):
                second.close()


def _delta(before: object, after: object) -> str:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return f"{after - before:+.4f}"
    return "N/A"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    config = load_config(args.config)
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    shared_state, redis_keys, privacy_evidence = _redis_evidence(config.cache.redis_url)

    test_log_path = Path("reports/test_log.txt")
    test_evidence = "Run `make test` with Redis running."
    if test_log_path.exists():
        nonempty = [line for line in test_log_path.read_text(encoding="utf-8").splitlines() if line]
        if nonempty:
            test_evidence = nonempty[-1]

    lines = [
        "# Day 25 - Track 3 - Reliability Engineering Final Report",
        "",
        "## 1. Architecture summary",
        "",
        (
            "The gateway checks a guarded response cache first. Cache misses traverse an ordered "
            "provider chain, with one three-state circuit breaker per provider. An open circuit "
            "fails fast and the next provider is tried; complete exhaustion returns a static, "
            "explicitly degraded response."
        ),
        "",
        "```text",
        "User -> ReliabilityGateway -> Cache (memory or Redis) -- hit --> Cached response",
        "                              | miss / cache outage",
        "                              v",
        "                       CircuitBreaker(primary) -> Primary provider",
        "                              | failure / OPEN",
        "                              v",
        "                       CircuitBreaker(backup)  -> Backup provider",
        "                              | failure / OPEN",
        "                              v",
        "                         Static fallback",
        "```",
        "",
        (
            "Every breaker records timestamped CLOSED/OPEN/HALF_OPEN transitions. "
            "Privacy-sensitive queries bypass both cache backends, and differing four-digit "
            "values are rejected as semantic false hits."
        ),
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Opens quickly enough to stop retry storms while tolerating two transient failures. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Bounds recovery detection without continuously probing an unhealthy provider. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | One successful probe closes the circuit for fast recovery in this local lab. |",
        f"| cache backend | {config.cache.backend} | Memory is the repeatable baseline; Redis is separately verified as shared state. |",
        f"| cache TTL | {config.cache.ttl_seconds}s | Limits staleness and memory growth while retaining repeated lab queries. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | Conservative threshold; the 2024/2026 guard catches high-scoring dated false hits. |",
        f"| load-test requests | {config.load_test.requests} per scenario | Large enough to exercise cache reuse and state transitions reproducibly. |",
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {_format(metrics.get('availability'))} | {_met(metrics.get('availability'), 0.99, minimum=True)} |",
        f"| Latency P95 | < 2500 ms | {_format(metrics.get('latency_p95_ms'))} ms | {_met(metrics.get('latency_p95_ms'), 2500, minimum=False)} |",
        f"| Fallback success rate | >= 95% | {_format(metrics.get('fallback_success_rate'))} | {_met(metrics.get('fallback_success_rate'), 0.95, minimum=True)} |",
        f"| Cache hit rate | >= 10% | {_format(metrics.get('cache_hit_rate'))} | {_met(metrics.get('cache_hit_rate'), 0.10, minimum=True)} |",
        f"| Recovery time | < 5000 ms | {_format(metrics.get('recovery_time_ms'))} ms | {_met(metrics.get('recovery_time_ms'), 5000, minimum=False)} |",
        "",
        (
            "The availability denominator includes every chaos request. A static fallback is "
            "counted as degraded/unavailable, rather than hiding provider exhaustion as success."
        ),
        "",
        "## 4. Metrics",
        "",
        "These values are generated by `make run-chaos`; they are not hand-entered.",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    metric_names = (
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "estimated_cost",
        "estimated_cost_saved",
        "circuit_open_count",
        "recovery_time_ms",
    )
    lines.extend(f"| {name} | {_format(metrics.get(name))} |" for name in metric_names)

    lines += [
        "",
        (
            "The latency distribution measures end-to-end gateway time, including failed-primary "
            "work before a backup succeeds. Cost saved uses the simulator's documented $0.001 "
            "avoided-call estimate for each cache hit."
        ),
        "",
        "## 5. Cache comparison",
        "",
        "The two runs use healthy providers, the same query workload, and the same request count.",
        "",
        "| Metric | Without cache | With cache | Delta (with - without) |",
        "|---|---:|---:|---:|",
    ]
    for name in ("latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"):
        before = without_cache.get(name)
        after = with_cache.get(name)
        lines.append(f"| {name} | {_format(before)} | {_format(after)} | {_delta(before, after)} |")
    lines += [
        "",
        (
            "A concrete false-hit test stores `refund policy for 2024` and queries `refund policy "
            "for 2026`. Similarity is high, but the differing year guard rejects it and records "
            "`date_or_number_mismatch`. Balance, password, credit-card, SSN, user and account "
            "queries are never stored."
        ),
        "",
        "## 6. Redis shared cache",
        "",
        (
            "An in-memory cache is process-local, so replicas cannot reuse each other's results "
            "and their state disappears on restart. `SharedRedisCache` uses deterministic hashed "
            "keys, Redis hashes, server-side TTL, SCAN-based similarity lookup, and identical "
            "privacy/false-hit guardrails. Cache read/write errors are isolated so providers "
            "remain available."
        ),
        "",
        "### Evidence of shared state",
        "",
        "```text",
        shared_state,
        f"privacy_guard={privacy_evidence}",
        f"test_log={test_evidence}",
        "```",
        "",
        "### Redis key evidence captured before cleanup",
        "",
        "```text",
        redis_keys,
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    scenario_results = metrics.get("scenario_results", {})
    statuses = metrics.get("scenarios", {})
    for scenario in config.scenarios:
        result = scenario_results.get(scenario.name, {})
        observed = (
            f"availability={_format(result.get('availability'))}; "
            f"fallback_rate={_format(result.get('fallback_success_rate'))}; "
            f"opens={_format(result.get('circuit_open_count'))}; "
            f"recovery_ms={_format(result.get('recovery_time_ms'))}"
        )
        description = scenario.description.replace("|", "\\|")
        lines.append(
            f"| {scenario.name} | {description} | {observed} | "
            f"{str(statuses.get(scenario.name, 'missing')).upper()} |"
        )

    lines += [
        "",
        (
            "The recovery scenario changes the primary from 100% failure to healthy during the "
            "run. A later HALF_OPEN probe succeeds, yielding an OPEN-to-CLOSED recovery "
            "measurement in the transition log. Scenario status is based on explicit "
            "behavior-specific criteria, not merely whether the script completed."
        ),
        "",
        "## 8. Failure analysis",
        "",
        (
            "The largest remaining production weakness is that circuit state is local to each "
            "gateway process. With many replicas, each replica can independently send its "
            "threshold of failing calls, multiplying load on an unhealthy provider. Redis "
            "similarity lookup also scans every cache key, which becomes O(n) at scale."
        ),
        "",
        (
            "Before production, breaker state and probe ownership should use an atomic shared "
            "store or service mesh, with jittered reset windows. Similarity search should move "
            "to an indexed vector store or Redis Search, with tenant-scoped keys, encryption, "
            "rate limits, and quality evaluation for false positives."
        ),
        "",
        "## 9. Next steps",
        "",
        "1. Share circuit counters and a single HALF_OPEN probe lease across replicas using atomic Redis operations.",
        "2. Add concurrent load tests and property-based state-machine tests for races and transition invariants.",
        "3. Add per-tenant cost budgets, cache namespaces, and alerts tied to the SLO table above.",
        "",
        "Artifacts: `reports/metrics.json`, `reports/metrics.csv`, and `reports/test_log.txt`.",
    ]

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
