"""KIS 자동매매 — 한국투자증권 OpenAPI 기반 (모의/실전 양쪽).

기본 환경은 모의(vts), 기본 동작은 dry-run. 자세한 설계는 ../../PLAN.md 참고.
"""
__all__ = ["config", "auth", "client", "market", "orders", "safety"]
