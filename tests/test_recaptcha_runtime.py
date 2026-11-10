import json
import shutil
import subprocess
import types
import asyncio
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from src.core.config import Config
from src.services.browser_captcha import BrowserCaptchaService, TokenBrowser, _BrowserRequest
from src.services.recaptcha_runtime import (
    TRUSTED_RECAPTCHA_HOOK_SOURCE,
    build_recaptcha_execute_script,
)


def _slot_concurrency(value):
    return patch.object(
        Config, "browser_slot_concurrency", new_callable=PropertyMock, return_value=value
    )


class LocalCaptchaActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = _slot_concurrency(1)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_protocol_actions_execute_without_native_generation(self):
        for action in ("IMAGE_GENERATION", "VIDEO_GENERATION", "UPLOAD_IMAGE"):
            with self.subTest(action=action):
                browser = object.__new__(TokenBrowser)
                browser.token_id = 7
                browser._apply_browser_environment_patch = AsyncMock()
                browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
                browser._harvest_token_via_ui_submit = AsyncMock()
                page = types.SimpleNamespace(
                    add_init_script=AsyncMock(),
                    evaluate=AsyncMock(return_value={"ok": True, "token": "captcha"}),
                    close=AsyncMock(),
                )
                context = types.SimpleNamespace(new_page=AsyncMock(return_value=page))
                token = await browser._execute_captcha(
                    context, "project", "site-key", action
                )
                self.assertEqual(token, "captcha")
                self.assertIn(json.dumps(action), page.evaluate.call_args.args[0])
                browser._harvest_token_via_ui_submit.assert_not_awaited()
                self.assertIsNone(browser._last_harvest_session_id)
                page.close.assert_awaited_once()

    async def test_execution_failure_closes_page_without_chat_fallback(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        browser._apply_browser_environment_patch = AsyncMock()
        browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
        browser._harvest_token_via_ui_submit = AsyncMock()
        page = types.SimpleNamespace(
            add_init_script=AsyncMock(),
            evaluate=AsyncMock(side_effect=RuntimeError("execution failed")),
            close=AsyncMock(),
        )
        token = await browser._execute_captcha(
            types.SimpleNamespace(new_page=AsyncMock(return_value=page)),
            "project",
            "site-key",
            "IMAGE_GENERATION",
        )
        self.assertIsNone(token)
        browser._harvest_token_via_ui_submit.assert_not_awaited()
        page.close.assert_awaited_once()

    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the JavaScript contract"
    )
    def test_shared_execute_script_preserves_requested_action(self):
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  const sandbox = { setTimeout, clearTimeout, location: { href: 'https://flow.google.com/project/example' } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(input.hook, sandbox);
  vm.runInContext(`
    globalThis.calls = [];
    globalThis.grecaptcha = { enterprise: {
      execute: function(key, options) { calls.push({ key, action: options.action }); return Promise.resolve('captcha'); }
    }};
    const original = grecaptcha.enterprise.execute.bind(grecaptcha.enterprise);
    grecaptcha.enterprise.execute = function(key, options) {
      return original(key, Object.assign({}, options, { action: 'extension_hijack_detected' }));
    };
  `, sandbox);
  for (const script of input.scripts) {
    const result = await vm.runInContext(script, sandbox);
    if (!result.ok || result.token !== 'captcha') throw Error('captcha execution failed');
  }
  const actual = sandbox.calls.map(call => call.action);
  if (JSON.stringify(actual) !== JSON.stringify(input.actions)) throw Error('action mismatch');
  if (sandbox.calls.some(call => call.key !== input.siteKey)) throw Error('site key mismatch');
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        actions = [
            "IMAGE_GENERATION",
            "VIDEO_GENERATION",
            "UPLOAD_IMAGE",
            "CHAT_GENERATION",
        ]
        site_key = 'site-key"\\value'
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps(
                {
                    "hook": TRUSTED_RECAPTCHA_HOOK_SOURCE,
                    "scripts": [
                        build_recaptcha_execute_script(site_key, action, 1000)
                        for action in actions
                    ],
                    "actions": actions,
                    "siteKey": site_key,
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the JavaScript contract"
    )
    def test_shared_execute_script_falls_back_to_direct_execute(self):
        """新版 Flow 不再调用 execute.bind()，hook 捕获不到闭包时必须直接执行。"""
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  const sandbox = { setTimeout, clearTimeout, location: { href: 'https://flow.google.com/project/example' } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(input.hook, sandbox);
  vm.runInContext(`
    globalThis.calls = [];
    globalThis.grecaptcha = { enterprise: {
      execute: function(key, options) {
        calls.push({ thisOK: this === grecaptcha.enterprise, key, action: options.action });
        return Promise.resolve('captcha');
      }
    }};
  `, sandbox);
  if (sandbox.__flow2apiTrustedRecaptchaExecute) throw Error('closure should not be captured');
  const result = await vm.runInContext(input.script, sandbox);
  if (!result.ok || result.token !== 'captcha') throw Error('captcha execution failed: ' + JSON.stringify(result));
  if (result.debug.captured !== false) throw Error('captured flag should be false');
  if (sandbox.calls.length !== 1) throw Error('expected one execute call, got ' + sandbox.calls.length);
  const call = sandbox.calls[0];
  if (!call.thisOK) throw Error('execute must be invoked with enterprise as receiver');
  if (call.key !== input.siteKey) throw Error('site key mismatch');
  if (call.action !== input.action) throw Error('action mismatch');
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        site_key = "site-key"
        action = "IMAGE_GENERATION"
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps(
                {
                    "hook": TRUSTED_RECAPTCHA_HOOK_SOURCE,
                    "script": build_recaptcha_execute_script(site_key, action, 1000),
                    "siteKey": site_key,
                    "action": action,
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


def _warm_browser():
    browser = object.__new__(TokenBrowser)
    browser.token_id = 7
    browser._warm_pages = {}
    browser._warm_page_locks = {}
    browser._runtime_generation = 0
    browser._harvest_streamchat_post = None
    browser._last_harvest_session_id = None
    browser._apply_browser_environment_patch = AsyncMock()
    browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
    pages = []

    def make_page():
        page = types.SimpleNamespace(
            add_init_script=AsyncMock(),
            evaluate=AsyncMock(return_value={"ok": True, "token": "tok"}),
            close=AsyncMock(),
            is_closed=lambda: False,
            url="https://flow.google.com/project/p",
        )
        pages.append(page)
        return page

    context = types.SimpleNamespace(new_page=AsyncMock(side_effect=lambda: make_page()))
    return browser, context, pages


class WarmPageReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = _slot_concurrency(4)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_sequential_tokens_reuse_one_page(self):
        browser, context, pages = _warm_browser()
        for _ in range(3):
            token = await browser._execute_captcha(context, "p", "key", "IMAGE_GENERATION")
            self.assertEqual(token, "tok")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].evaluate.await_count, 3)
        pages[0].close.assert_not_awaited()
        browser._prepare_flow_runtime_page.assert_awaited_once()

    async def test_concurrent_tokens_open_a_single_page(self):
        browser, context, pages = _warm_browser()
        tokens = await asyncio.gather(
            *[
                browser._execute_captcha(context, "p", "key", "IMAGE_GENERATION")
                for _ in range(4)
            ]
        )
        self.assertEqual(tokens, ["tok"] * 4)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].evaluate.await_count, 4)

    async def test_failed_execute_drops_the_page_and_next_call_reopens(self):
        browser, context, pages = _warm_browser()
        self.assertEqual(
            await browser._execute_captcha(context, "p", "key", "UPLOAD_IMAGE"), "tok"
        )
        pages[0].evaluate = AsyncMock(return_value={"ok": False, "error": "boom"})
        self.assertIsNone(
            await browser._execute_captcha(context, "p", "key", "UPLOAD_IMAGE")
        )
        pages[0].close.assert_awaited_once()
        self.assertEqual(browser._warm_pages, {})
        self.assertEqual(
            await browser._execute_captcha(context, "p", "key", "UPLOAD_IMAGE"), "tok"
        )
        self.assertEqual(len(pages), 2)

    async def test_page_from_recycled_browser_is_replaced(self):
        browser, context, pages = _warm_browser()
        await browser._execute_captcha(context, "p", "key", "IMAGE_GENERATION")
        browser._runtime_generation += 1
        await browser._execute_captcha(context, "p", "key", "IMAGE_GENERATION")
        self.assertEqual(len(pages), 2)
        pages[0].close.assert_awaited_once()

    async def test_chat_generation_never_uses_the_warm_page(self):
        browser, context, pages = _warm_browser()
        browser._harvest_token_via_ui_submit = AsyncMock(return_value=None)
        browser._apply_browser_environment_patch = AsyncMock()
        await browser._execute_captcha(context, "p", "key", "CHAT_GENERATION")
        self.assertEqual(browser._warm_pages, {})


class SlotCapacityTests(unittest.TestCase):
    def _service(self):
        service = object.__new__(BrowserCaptchaService)
        service._slot_recovery = {}
        service._requests = {}
        service._slot_reservations = {}
        service._browsers = {}
        return service

    def _request(self, ref, slot_id, borrowed):
        return _BrowserRequest(
            ref=ref,
            slot_id=slot_id,
            browser=None,
            token_id=1,
            generation=0,
            owner=None,
            borrower=object() if borrowed else None,
        )

    def test_slot_accepts_requests_up_to_capacity(self):
        with _slot_concurrency(4):
            service = self._service()
            for count in range(4):
                self.assertFalse(service._is_slot_busy_for_allocation(0), count)
                service._reserve_slot_locked(0)
            self.assertTrue(service._is_slot_busy_for_allocation(0))

    def test_held_leases_count_once_and_free_slots_when_released(self):
        with _slot_concurrency(2):
            service = self._service()
            # one request being borrowed (reservation + request) and one held lease
            service._reserve_slot_locked(0)
            service._requests["a"] = self._request("a", 0, borrowed=True)
            service._requests["b"] = self._request("b", 0, borrowed=False)
            self.assertTrue(service._is_slot_busy_for_allocation(0))
            service._requests.pop("b")
            self.assertFalse(service._is_slot_busy_for_allocation(0))

    def test_capacity_one_keeps_exclusive_slot(self):
        with _slot_concurrency(1):
            service = self._service()
            self.assertFalse(service._is_slot_busy_for_allocation(0))
            service._requests["a"] = self._request("a", 0, borrowed=False)
            self.assertTrue(service._is_slot_busy_for_allocation(0))

    def test_recovering_slot_is_busy_regardless_of_capacity(self):
        with _slot_concurrency(4):
            service = self._service()
            service._slot_recovery[0] = types.SimpleNamespace(phase="draining")
            self.assertTrue(service._is_slot_busy_for_allocation(0))


if __name__ == "__main__":
    unittest.main()
