import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import routes


def make_handler(tokens, rows, refreshed=240):
    db = SimpleNamespace(
        get_all_tokens_with_stats=AsyncMock(return_value=tokens),
        get_completed_image_models_since=AsyncMock(return_value=rows),
    )
    token_manager = SimpleNamespace(refresh_credits=AsyncMock(return_value=refreshed))
    return SimpleNamespace(db=db, token_manager=token_manager)


class UsageEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        routes._credits_refreshed_at.clear()

    async def test_counts_by_bucket_and_reports_credits(self):
        handler = make_handler(
            [{"id": 1, "is_active": 1, "credits": 250, "user_paygate_tier": "PAYGATE_TIER_ONE"}],
            [(1, "gemini-3.0-pro-image-landscape"), (1, "gemini-3.1-flash-image-landscape"),
             (1, "gemini-3.1-flash-image-portrait-2k")],
        )
        with patch.object(routes, "generation_handler", handler):
            usage = await routes.get_usage(api_key="k")
        pro, other = usage["image_daily"]
        self.assertEqual((pro["used"], pro["limit"]), (1, 100))
        self.assertEqual((other["used"], other["limit"]), (2, 1000))
        self.assertEqual(usage["credits"], 240)

    async def test_ultra_limits_and_inactive_tokens_ignored(self):
        handler = make_handler(
            [{"id": 1, "is_active": 1, "credits": 10, "user_paygate_tier": "PAYGATE_TIER_TWO"},
             {"id": 2, "is_active": 0, "credits": 99, "user_paygate_tier": "PAYGATE_TIER_ONE"}],
            [],
        )
        with patch.object(routes, "generation_handler", handler):
            usage = await routes.get_usage(api_key="k")
        self.assertEqual([d["limit"] for d in usage["image_daily"]], [500, 1000])
        handler.token_manager.refresh_credits.assert_awaited_once_with(1)

    async def test_refresh_failure_falls_back_to_stored_credits(self):
        handler = make_handler(
            [{"id": 1, "is_active": 1, "credits": 250, "user_paygate_tier": "PAYGATE_TIER_ONE"}], []
        )
        handler.token_manager.refresh_credits.side_effect = RuntimeError("offline")
        with patch.object(routes, "generation_handler", handler):
            usage = await routes.get_usage(api_key="k")
        self.assertEqual(usage["credits"], 250)


if __name__ == "__main__":
    unittest.main()
