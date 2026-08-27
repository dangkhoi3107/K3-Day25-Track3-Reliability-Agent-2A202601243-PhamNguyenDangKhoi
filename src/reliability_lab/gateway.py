from __future__ import annotations

from dataclasses import dataclass

from redis.exceptions import RedisError

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Route through cache, protected providers, and the static fallback."""
        last_error: str | None = None
        if self.cache is not None:
            try:
                cached_text, score = self.cache.get(prompt)
            except RedisError as exc:
                # A cache outage must not take down the provider path.
                cached_text, score = None, 0.0
                last_error = f"cache read: {exc}"
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = f"{provider.name}: {exc}"
                continue

            if self.cache is not None:
                try:
                    self.cache.set(
                        prompt,
                        response.text,
                        {
                            "provider": provider.name,
                            "estimated_cost": str(response.estimated_cost),
                        },
                    )
                except RedisError as exc:
                    # Cache writes are best effort; the fresh provider response is still valid.
                    last_error = f"cache write: {exc}"

            return GatewayResponse(
                text=response.text,
                route="primary" if index == 0 else "fallback",
                provider=response.provider,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
                error=last_error,
            )

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error or "no providers configured",
        )
