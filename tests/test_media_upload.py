import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.api import routes

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    async def body(self) -> bytes:
        return self._body


class MediaUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(routes, "UPLOAD_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    async def test_upload_then_resolve_ref(self):
        first = await routes.upload_media(FakeRequest(PNG_BYTES), api_key="k")
        digest = hashlib.sha256(PNG_BYTES).hexdigest()
        self.assertEqual(first["id"], digest)
        self.assertEqual(first["uri"], f"ref://{digest}")
        self.assertFalse(first["existed"])

        again = await routes.upload_media(FakeRequest(PNG_BYTES), api_key="k")
        self.assertTrue(again["existed"])

        self.assertEqual(await routes._load_image_bytes_from_uri(first["uri"]), PNG_BYTES)
        self.assertEqual((await routes.head_media(digest, api_key="k")).status_code, 200)

    async def test_missing_ref_is_404(self):
        missing = "0" * 64
        with self.assertRaises(HTTPException) as ctx:
            await routes._load_image_bytes_from_uri(f"ref://{missing}")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            await routes.head_media(missing, api_key="k")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_rejects_bad_id_and_non_image(self):
        with self.assertRaises(HTTPException) as ctx:
            await routes._load_image_bytes_from_uri("ref://../../config/setting")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            await routes.upload_media(FakeRequest(b"not an image"), api_key="k")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
