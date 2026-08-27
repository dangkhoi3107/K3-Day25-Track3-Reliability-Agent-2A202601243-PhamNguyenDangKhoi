from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig, provider_overrides: dict[str, float] | None = None
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = (
            provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        )
        providers.append(
            FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens)
        )
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Return mean OPEN-to-CLOSED recovery time across all completed recoveries."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            destination = transition["to"]
            timestamp = float(transition["ts"])
            if destination == "open" and opened_at is None:
                opened_at = timestamp
            elif destination == "closed" and opened_at is not None:
                recovery_times.append((timestamp - opened_at) * 1000.0)
                opened_at = None

    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run one deterministic named scenario and collect end-to-end metrics."""
    if not queries:
        raise ValueError("at least one query is required")

    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    seed_material = f"day25:{scenario.name}".encode()
    seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
    query_rng = random.Random(seed)
    random.seed(seed)

    for request_index in range(config.load_test.requests):
        if (
            scenario.recovery_after_requests is not None
            and request_index == scenario.recovery_after_requests
            and scenario.recovery_provider is not None
        ):
            for provider in gateway.providers:
                if provider.name == scenario.recovery_provider:
                    provider.fail_rate = scenario.recovery_fail_rate
                    break

        prompt = query_rng.choice(queries)
        started_at = time.perf_counter()
        result = gateway.complete(prompt)
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0

        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        metrics.latencies_ms.append(elapsed_ms)

        if result.cache_hit:
            metrics.cache_hits += 1
            # The simulator uses $0.001 as the documented avoided-call estimate.
            metrics.estimated_cost_saved += 0.001

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)

    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics


def _scenario_passed(scenario: ScenarioConfig, result: RunMetrics, provider_count: int) -> bool:
    """Evaluate whether observed behavior matches the scenario's reliability goal."""
    if scenario.recovery_after_requests is not None:
        return result.availability >= 0.95 and result.recovery_time_ms is not None
    if scenario.name == "primary_timeout_100":
        return (
            result.availability >= 0.95
            and result.fallback_successes > 0
            and result.circuit_open_count >= 1
        )
    if scenario.name == "all_healthy":
        return result.availability == 1.0 and result.static_fallbacks == 0
    if scenario.name == "all_providers_down":
        return result.static_fallbacks > 0 and result.circuit_open_count >= provider_count
    return result.availability >= 0.95


def _scenario_snapshot(result: RunMetrics) -> dict[str, object]:
    """Keep the per-scenario evidence needed by the final report."""
    report = result.to_report_dict()
    keys = (
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
    )
    return {key: report[key] for key in keys}


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run named chaos scenarios plus a controlled cache comparison."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        metrics.scenario_results = {"default": _scenario_snapshot(metrics)}
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        passed = _scenario_passed(scenario, result, len(config.providers))
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_results[scenario.name] = _scenario_snapshot(result)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)

    comparison_scenario = ScenarioConfig(
        name="cache_comparison_healthy",
        description="Controlled healthy-provider cache comparison",
        provider_overrides={provider.name: 0.0 for provider in config.providers},
    )
    without_cache_config = config.model_copy(deep=True)
    without_cache_config.cache.enabled = False
    without_cache = run_scenario(without_cache_config, queries, comparison_scenario)

    with_cache_config = config.model_copy(deep=True)
    with_cache_config.cache.enabled = True
    with_cache = run_scenario(with_cache_config, queries, comparison_scenario)
    combined.cache_comparison = {
        "without_cache": _scenario_snapshot(without_cache),
        "with_cache": _scenario_snapshot(with_cache),
    }

    return combined
