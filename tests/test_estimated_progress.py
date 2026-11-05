import asyncio
import time
import unittest
from unittest.mock import AsyncMock

from src.services.generation_handler import GenerationHandler


def make_handler(durations_by_model=None, durations_all=None):
    handler = GenerationHandler.__new__(GenerationHandler)
    handler.db = AsyncMock()

    async def recent(operation, model=None, limit=10):
        return (durations_by_model if model else durations_all) or []

    handler.db.get_recent_request_durations = recent
    return handler


class EstimatedProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimate_uses_model_median_then_operation_then_default(self):
        handler = make_handler([50, 60, 70, 300], [10, 10, 10])
        self.assertEqual(await handler._estimate_duration("m", "generate_image"), 65)
        handler = make_handler([50], [20, 30, 40])
        self.assertEqual(await handler._estimate_duration("m", "generate_image"), 30)
        handler = make_handler([], [])
        self.assertEqual(await handler._estimate_duration("m", "generate_image"), 60.0)
        self.assertEqual(await handler._estimate_duration("m", "generate_video"), 150.0)

    def test_percent_curve_never_reaches_100(self):
        now = time.time()
        pct = lambda elapsed: GenerationHandler._estimated_percent(
            {"started_at": now - elapsed, "expected": 60}
        )
        self.assertEqual(pct(0), 0)
        self.assertEqual(pct(30), 50)
        self.assertEqual(pct(54), 90)
        self.assertLessEqual(pct(60), 99)
        self.assertEqual(pct(600), 98)
        self.assertTrue(all(pct(t) <= pct(t + 1) for t in range(0, 300)))

    async def test_progress_chunks_emit_until_task_done(self):
        handler = make_handler()
        handler._create_stream_chunk = lambda text, **_: text
        state = {"started_at": time.time(), "expected": 10}
        task = asyncio.ensure_future(asyncio.sleep(1.5, result="done"))
        chunks = [c async for c in handler._progress_chunks(task, state, True)]
        self.assertGreaterEqual(len(chunks), 1)
        self.assertTrue(all("生成进度: 约" in c and c.endswith("%\n") for c in chunks))
        self.assertEqual(await task, "done")

        task = asyncio.ensure_future(asyncio.sleep(0.05, result="x"))
        self.assertEqual([c async for c in handler._progress_chunks(task, state, False)], [])
        self.assertEqual(await task, "x")


if __name__ == "__main__":
    unittest.main()
