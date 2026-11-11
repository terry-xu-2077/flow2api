"""Message codes for the streamed progress/status lines.

Every non-final stream chunk carries the old human readable text (``reasoning_content``) and, next to it,
``"status": {"code": ..., "params": {...}}`` so a client can format the message itself. Codes are derived from
the text in one place (here) instead of at every call site; text that matches nothing becomes
``{"code": "message", "params": {"text": ...}}`` so no status line is ever lost.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

Params = Dict[str, Any]


def _kind(word: str) -> str:
    return "video" if word == "视频" else "image"


# (pattern, code, params-from-match). First match wins, patterns are anchored at the start of the line.
PATTERNS: List[Tuple["re.Pattern[str]", str, Callable[["re.Match[str]"], Params]]] = [
    (re.compile(r"生成进度: 约 (\d+)%"), "progress", lambda m: {"percent": int(m[1])}),
    (re.compile(r"✨ (视频|图片)生成任务已启动"), "task_started", lambda m: {"kind": _kind(m[1])}),
    (re.compile(r"⏳ 排队中：当前 (\d+) 个图片任务正在生成，您排在第 (\d+) 位"), "queued",
     lambda m: {"active": int(m[1]), "position": int(m[2])}),
    (re.compile(r"初始化生成环境"), "init_env", lambda m: {}),
    (re.compile(r"当前账号配额已用尽"), "account_switch", lambda m: {"reason": "quota"}),
    (re.compile(r"当前账号触发风控"), "account_switch", lambda m: {"reason": "risk"}),
    (re.compile(r"上传 (\d+) 张 Omni 参考图片"), "refs_uploading", lambda m: {"count": int(m[1]), "omni": True}),
    (re.compile(r"上传 (\d+) 张参考图片"), "refs_uploading", lambda m: {"count": int(m[1])}),
    (re.compile(r"已上传第 (\d+)/(\d+) 张图片"), "ref_uploaded", lambda m: {"index": int(m[1]), "total": int(m[2])}),
    (re.compile(r"上传首帧和尾帧图片"), "frames_uploading", lambda m: {"first": True, "last": True}),
    (re.compile(r"上传首帧图片"), "frames_uploading", lambda m: {"first": True, "last": False}),
    (re.compile(r"参考图片上传完成，正在进行打码验证"), "captcha_after_upload", lambda m: {}),
    (re.compile(r"正在进行打码验证并提交图片生成请求"), "captcha_submit", lambda m: {}),
    (re.compile(r"正在放大图片到 (\w+)"), "upscale_start", lambda m: {"resolution": m[1], "kind": "image"}),
    (re.compile(r"✅ 图片已放大到 (\w+)"), "upscale_done", lambda m: {"resolution": m[1]}),
    (re.compile(r"⚠️ 放大遇到(.+?)，正在重试 \((\d+)/(\d+)\)"), "upscale_retry",
     lambda m: {"reason": m[1], "attempt": int(m[2]), "max": int(m[3])}),
    (re.compile(r"⚠️ 放大失败，返回原图"), "upscale_failed", lambda m: {"fallback": "origin"}),
    (re.compile(r"⚠️ 放大失败: (.*?)，返回(原图|原始视频)", re.S), "upscale_failed",
     lambda m: {"error": m[1], "fallback": "origin"}),
    (re.compile(r"⚠️ 放大任务创建失败，返回原始视频"), "upscale_failed", lambda m: {"fallback": "origin", "kind": "video"}),
    (re.compile(r"缓存 (\w+) 图片中"), "cache_start", lambda m: {"resolution": m[1], "kind": "image"}),
    (re.compile(r"正在缓存 (\w+) 图片文件"), "cache_start", lambda m: {"resolution": m[1], "kind": "image"}),
    (re.compile(r"正在缓存视频文件"), "cache_start", lambda m: {"kind": "video"}),
    (re.compile(r"✅ (\w+) 图片缓存成功"), "cache_done", lambda m: {"resolution": m[1], "kind": "image"}),
    (re.compile(r"✅ 视频缓存成功"), "cache_done", lambda m: {"kind": "video"}),
    (re.compile(r"⚠️ 缓存失败: (.*?)(?:，返回内联图片|\n正在返回源链接|$)", re.S), "cache_failed", lambda m: {"error": m[1]}),
    (re.compile(r"缓存已关闭"), "cache_disabled", lambda m: {}),
    (re.compile(r"⚠️ 文生视频模型不支持上传图片"), "images_ignored", lambda m: {"kind": "video"}),
    (re.compile(r"提交 Omni 参考图视频任务"), "video_submit", lambda m: {"omni": True}),
    (re.compile(r"提交视频生成任务"), "video_submit", lambda m: {}),
    (re.compile(r"视频续写任务提交中，源视频: (\S+?)\.\.\."), "video_extend_submit", lambda m: {"source": m[1]}),
    (re.compile(r"视频生成中"), "video_generating", lambda m: {}),
    (re.compile(r"视频生成完成，正在下载视频文件"), "video_downloading", lambda m: {}),
    (re.compile(r"\n?视频生成完成，开始 (\w+) 放大处理"), "upscale_start", lambda m: {"resolution": m[1], "kind": "video"}),
    (re.compile(r"放大任务已提交，继续轮询"), "upscale_polling", lambda m: {}),
    (re.compile(r"续写片段已生成"), "video_extend_done", lambda m: {}),
    (re.compile(r"错误: (.*)", re.S), "error", lambda m: {"message": m[1].strip()}),
]


def classify(text: str) -> Tuple[str, Params]:
    """Return ``(code, params)`` for one status line; unknown text maps to the generic ``message`` code."""
    body = text if isinstance(text, str) else str(text)
    for pattern, code, extract in PATTERNS:
        match = pattern.match(body)
        if match:
            return code, extract(match)
    return "message", {"text": body.strip()}


def status_payload(text: str) -> Optional[Dict[str, Any]]:
    """The ``status`` object for a stream chunk (None for blank keep-alive text)."""
    if not (text or "").strip():
        return None
    code, params = classify(text)
    return {"code": code, "params": params}
