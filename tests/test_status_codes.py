import json
import unittest
from unittest.mock import PropertyMock, patch

from src.core.config import Config
from src.services.generation_handler import GenerationHandler
from src.services.status_codes import classify, status_payload

# every status line the handler can stream, with the code/params a client should get
CASES = [
    ("生成进度: 约 42%\n", "progress", {"percent": 42}),
    ("✨ 图片生成任务已启动\n", "task_started", {"kind": "image"}),
    ("✨ 视频生成任务已启动\n", "task_started", {"kind": "video"}),
    ("⏳ 排队中：当前 4 个图片任务正在生成，您排在第 2 位...\n", "queued", {"active": 4, "position": 2}),
    ("初始化生成环境...\n", "init_env", {}),
    ("当前账号配额已用尽，正在切换其他账号重试...\n", "account_switch", {"reason": "quota"}),
    ("当前账号触发风控，正在切换其他账号重试...\n", "account_switch", {"reason": "risk"}),
    ("上传 3 张参考图片...\n", "refs_uploading", {"count": 3}),
    ("上传 2 张 Omni 参考图片...\n", "refs_uploading", {"count": 2, "omni": True}),
    ("已上传第 1/3 张图片\n", "ref_uploaded", {"index": 1, "total": 3}),
    ("上传首帧图片...\n", "frames_uploading", {"first": True, "last": False}),
    ("上传首帧和尾帧图片...\n", "frames_uploading", {"first": True, "last": True}),
    ("参考图片上传完成，正在进行打码验证...\n", "captcha_after_upload", {}),
    ("正在进行打码验证并提交图片生成请求...\n", "captcha_submit", {}),
    ("正在放大图片到 2K...\n", "upscale_start", {"resolution": "2K", "kind": "image"}),
    ("✅ 图片已放大到 4K\n", "upscale_done", {"resolution": "4K"}),
    ("⚠️ 放大遇到reCAPTCHA 验证失败，正在重试 (2/3)...\n", "upscale_retry", {"reason": "reCAPTCHA 验证失败", "attempt": 2, "max": 3}),
    ("⚠️ 放大失败，返回原图...\n", "upscale_failed", {"fallback": "origin"}),
    ("⚠️ 放大失败: boom，返回原图...\n", "upscale_failed", {"error": "boom", "fallback": "origin"}),
    ("⚠️ 放大失败: boom，返回原始视频\n", "upscale_failed", {"error": "boom", "fallback": "origin"}),
    ("⚠️ 放大任务创建失败，返回原始视频\n", "upscale_failed", {"fallback": "origin", "kind": "video"}),
    ("缓存 2K 图片中...\n", "cache_start", {"resolution": "2K", "kind": "image"}),
    ("正在缓存 1K 图片文件...\n", "cache_start", {"resolution": "1K", "kind": "image"}),
    ("正在缓存视频文件...\n", "cache_start", {"kind": "video"}),
    ("✅ 2K 图片缓存成功\n", "cache_done", {"resolution": "2K", "kind": "image"}),
    ("✅ 1K 图片缓存成功,准备返回缓存地址...\n", "cache_done", {"resolution": "1K", "kind": "image"}),
    ("✅ 视频缓存成功,准备返回缓存地址...\n", "cache_done", {"kind": "video"}),
    ("⚠️ 缓存失败: disk full，返回内联图片...\n", "cache_failed", {"error": "disk full"}),
    ("⚠️ 缓存失败: disk full\n正在返回源链接...\n", "cache_failed", {"error": "disk full"}),
    ("缓存已关闭,正在返回官方图片链接...\n", "cache_disabled", {}),
    ("⚠️ 文生视频模型不支持上传图片,将忽略图片仅使用文本提示词生成\n", "images_ignored", {"kind": "video"}),
    ("提交视频生成任务...\n", "video_submit", {}),
    ("提交 Omni 参考图视频任务...\n", "video_submit", {"omni": True}),
    ("视频续写任务提交中，源视频: abcd1234...\n", "video_extend_submit", {"source": "abcd1234"}),
    ("视频生成中...\n", "video_generating", {}),
    ("视频生成完成，正在下载视频文件...\n", "video_downloading", {}),
    ("\n视频生成完成，开始 4K 放大处理...（可能需要 30 分钟）\n", "upscale_start", {"resolution": "4K", "kind": "video"}),
    ("放大任务已提交，继续轮询...\n", "upscale_polling", {}),
    ("续写片段已生成；当前 Flow 前端未提供媒体拼接 RPC，返回续写片段。\n", "video_extend_done", {}),
    ("错误: 没有可用的Token\n", "error", {"message": "没有可用的Token"}),
    ("某条上游新增的提示\n", "message", {"text": "某条上游新增的提示"}),
]


class StatusCodeTests(unittest.TestCase):
    def test_every_known_status_line_has_a_code(self):
        for text, code, params in CASES:
            with self.subTest(text=text):
                self.assertEqual(classify(text), (code, params))

    def test_blank_keep_alive_text_has_no_status(self):
        self.assertIsNone(status_payload("\n"))
        self.assertEqual(status_payload("生成进度: 约 5%\n"), {"code": "progress", "params": {"percent": 5}})


class StreamChunkTests(unittest.TestCase):
    def _chunk(self, *args, **kwargs):
        handler = object.__new__(GenerationHandler)
        raw = handler._create_stream_chunk(*args, **kwargs)
        self.assertTrue(raw.startswith("data: "))
        return json.loads(raw[6:])

    def test_status_chunk_keeps_text_and_adds_the_code(self):
        with patch.object(Config, "generation_status_text", new_callable=PropertyMock, return_value=True):
            chunk = self._chunk("上传 3 张参考图片...\n")
        self.assertEqual(chunk["status"], {"code": "refs_uploading", "params": {"count": 3}})
        self.assertEqual(chunk["choices"][0]["delta"]["reasoning_content"], "上传 3 张参考图片...\n")

    def test_text_can_be_switched_off_but_the_code_stays(self):
        with patch.object(Config, "generation_status_text", new_callable=PropertyMock, return_value=False):
            chunk = self._chunk("生成进度: 约 60%\n")
        self.assertEqual(chunk["status"], {"code": "progress", "params": {"percent": 60}})
        self.assertNotIn("reasoning_content", chunk["choices"][0]["delta"])

    def test_final_result_chunk_is_untouched(self):
        with patch.object(Config, "generation_status_text", new_callable=PropertyMock, return_value=False):
            chunk = self._chunk("![Generated Image](http://x/a.png)", finish_reason="stop")
        self.assertNotIn("status", chunk)
        self.assertEqual(chunk["choices"][0]["delta"]["content"], "![Generated Image](http://x/a.png)")
        self.assertEqual(chunk["choices"][0]["finish_reason"], "stop")

    def test_role_is_kept_on_the_first_chunk(self):
        with patch.object(Config, "generation_status_text", new_callable=PropertyMock, return_value=False):
            chunk = self._chunk("✨ 图片生成任务已启动\n", role="assistant")
        self.assertEqual(chunk["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(chunk["status"]["code"], "task_started")


if __name__ == "__main__":
    unittest.main()


class ExplicitStatusTests(unittest.TestCase):
    def test_explicit_status_overrides_the_text_guess(self):
        handler = object.__new__(GenerationHandler)
        raw = handler._create_stream_chunk(
            "图片数量不符合要求\n", status={"code": "error", "params": {"message": "图片数量不符合要求"}}
        )
        chunk = json.loads(raw[6:])
        self.assertEqual(chunk["status"], {"code": "error", "params": {"message": "图片数量不符合要求"}})
