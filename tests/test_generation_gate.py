import asyncio
import json
import unittest
from unittest.mock import PropertyMock, patch

from src.core.config import Config
from src.services.generation_gate import GateRejected, GenerationGate
from src.services.generation_handler import GenerationHandler

IMAGE_MODEL = "gemini-3.1-flash-image-landscape"


def make_gate(limit=2, mode="queue", max_wait=30.0, max_queue=0):
    return GenerationGate(
        limit=lambda: limit, mode=lambda: mode,
        max_wait=lambda: max_wait, max_queue=lambda: max_queue,
    )


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_gate_holds_nothing(self):
        gate = make_gate(limit=0)
        self.assertFalse(await gate.acquire())
        self.assertEqual(gate.active, 0)

    async def test_waiters_are_released_in_fifo_order(self):
        gate = make_gate(limit=1)
        self.assertTrue(await gate.acquire())
        order = []

        async def worker(name):
            await gate.acquire()
            order.append(name)

        first = asyncio.create_task(worker("a"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(worker("b"))
        await asyncio.sleep(0.01)
        self.assertEqual(gate.waiting, 2)
        gate.release()
        await asyncio.wait_for(first, 1)
        self.assertEqual(order, ["a"])
        gate.release()
        await asyncio.wait_for(second, 1)
        self.assertEqual(order, ["a", "b"])
        self.assertEqual(gate.active, 1)
        gate.release()
        self.assertEqual(gate.active, 0)

    async def test_reject_mode_refuses_immediately(self):
        gate = make_gate(limit=1, mode="reject")
        await gate.acquire()
        with self.assertRaises(GateRejected) as ctx:
            await gate.acquire()
        self.assertEqual(ctx.exception.reason, "busy")
        self.assertEqual(gate.waiting, 0)

    async def test_queue_length_cap_rejects_extra_waiters(self):
        gate = make_gate(limit=1, max_queue=1)
        await gate.acquire()
        waiter = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0.01)
        with self.assertRaises(GateRejected) as ctx:
            await gate.acquire()
        self.assertEqual(ctx.exception.reason, "queue_full")
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

    async def test_wait_timeout_rejects_and_leaves_queue_clean(self):
        gate = make_gate(limit=1, max_wait=1.0)
        await gate.acquire()
        with self.assertRaises(GateRejected) as ctx:
            await gate.acquire(notify_interval=0.2)
        self.assertEqual(ctx.exception.reason, "wait_timeout")
        self.assertEqual(gate.waiting, 0)
        self.assertEqual(gate.active, 1)

    async def test_cancelled_waiter_does_not_leak_a_slot(self):
        gate = make_gate(limit=1)
        await gate.acquire()
        waiter = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0.01)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(gate.waiting, 0)
        gate.release()
        self.assertEqual(gate.active, 0)
        self.assertTrue(await gate.acquire())

    async def test_slot_handed_to_cancelled_waiter_is_passed_on(self):
        gate = make_gate(limit=1)
        await gate.acquire()
        first = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0.01)
        second = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0.01)
        gate.release()  # hands the slot to `first`
        first.cancel()  # ...which is cancelled before it can use it
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(second, 1)
        self.assertEqual(gate.active, 1)

    async def test_notify_callback_reports_position(self):
        gate = make_gate(limit=1)
        await gate.acquire()
        seen = []
        waiter = asyncio.create_task(
            gate.acquire(on_wait=lambda pos, active: seen.append((pos, active)),
                         notify_interval=0.05)
        )
        await asyncio.sleep(0.2)
        gate.release()
        await asyncio.wait_for(waiter, 1)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[0], (1, 1))


def _patched(limit, mode="queue", max_wait=30.0, max_queue=0):
    return [
        patch.object(Config, "image_max_concurrency", new_callable=PropertyMock, return_value=limit),
        patch.object(Config, "image_queue_mode", new_callable=PropertyMock, return_value=mode),
        patch.object(Config, "image_queue_max_wait_seconds", new_callable=PropertyMock, return_value=max_wait),
        patch.object(Config, "image_queue_max_length", new_callable=PropertyMock, return_value=max_queue),
    ]


class HandlerGateTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self, hold=0.05):
        handler = object.__new__(GenerationHandler)
        stats = {"running": 0, "peak": 0, "done": 0}

        async def inner(model, prompt, images, stream, base_url_override, video_media_id):
            stats["running"] += 1
            stats["peak"] = max(stats["peak"], stats["running"])
            try:
                await asyncio.sleep(hold)
                yield "result:" + prompt
                stats["done"] += 1
            finally:
                stats["running"] -= 1

        handler._handle_generation_inner = inner
        return handler, stats

    async def _run(self, handler, prompt, stream=False):
        return [c async for c in handler.handle_generation(IMAGE_MODEL, prompt, stream=stream)]

    async def test_never_more_than_limit_run_at_once(self):
        handler, stats = self._handler()
        patches = _patched(limit=2)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        results = await asyncio.gather(*[self._run(handler, f"p{i}") for i in range(6)])
        self.assertEqual(stats["peak"], 2)
        self.assertEqual(stats["done"], 6)
        self.assertEqual(sorted(r[-1] for r in results), sorted(f"result:p{i}" for i in range(6)))
        self.assertEqual(handler._image_gate.active, 0)

    async def test_streaming_waiters_get_queue_notices(self):
        handler, stats = self._handler(hold=0.8)
        patches = _patched(limit=1)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        a = asyncio.create_task(self._run(handler, "a", stream=True))
        await asyncio.sleep(0.05)
        b = await self._run(handler, "b", stream=True)
        await a
        text = "".join(b)
        self.assertIn("排队中", text)
        self.assertIn("result:b", text)

    async def test_reject_mode_returns_429_json(self):
        handler, stats = self._handler(hold=0.3)
        patches = _patched(limit=1, mode="reject")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        a = asyncio.create_task(self._run(handler, "a"))
        await asyncio.sleep(0.05)
        rejected = await self._run(handler, "b")
        payload = json.loads(rejected[0])
        self.assertEqual(payload["error"]["status_code"], 429)
        self.assertEqual(payload["error"]["code"], "server_busy")
        await a
        self.assertEqual(handler._image_gate.active, 0)

    async def test_slot_freed_when_client_closes_the_stream_early(self):
        handler, stats = self._handler(hold=5)
        patches = _patched(limit=1)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        agen = handler.handle_generation(IMAGE_MODEL, "x", stream=False)
        task = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0.05)
        self.assertEqual(handler._image_gate.active, 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agen.aclose()
        self.assertEqual(handler._image_gate.active, 0)

    async def test_disabled_limit_and_non_image_models_bypass_the_gate(self):
        handler, stats = self._handler()
        patches = _patched(limit=0)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        await self._run(handler, "x")
        self.assertIsNone(getattr(handler, "_image_gate", None))


if __name__ == "__main__":
    unittest.main()
