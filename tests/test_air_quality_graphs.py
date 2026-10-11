import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

from telegram import InputFile, InputMediaPhoto

from api import air_quality as aq


class GraphsTests(unittest.IsolatedAsyncioTestCase):
    async def run_graphs(self, nea=None, aqicn=None, nea_error=None,
                         aqicn_error=None, render_error=None):
        message = SimpleNamespace(
            reply_text=AsyncMock(), reply_photo=AsyncMock(),
            reply_media_group=AsyncMock(),
        )
        if nea is None:
            nea = [{"PM2.5": {"North": 20}, "PM10": {"North": 10}}]
        if aqicn is None:
            aqicn = [{"region": "North", "pm25": {"min": 10, "max": 30, "current": 20}}]
        with (
            patch.object(aq, "get_singapore_pm_history", return_value=nea, side_effect=nea_error),
            patch.object(aq, "get_aqicn_pm_graphs", return_value=aqicn, side_effect=aqicn_error),
            patch.object(aq, "_render_singapore_pm_graph", return_value=b"nea png", side_effect=render_error),
            patch.object(aq, "_render_aqicn_graph_panel", return_value=b"aqicn png"),
            patch.object(aq.logger, "warning"),
        ):
            await aq.graphs(SimpleNamespace(effective_message=message), None)
        return message

    async def test_both_sources_send_serializable_telegram_album(self):
        message = await self.run_graphs()
        media = message.reply_media_group.call_args.kwargs["media"]
        self.assertEqual(len(media), 2)
        for photo in media:
            self.assertIsInstance(photo, InputMediaPhoto)
            self.assertIsInstance(photo.media, InputFile)
            self.assertTrue(photo.media.attach_uri.startswith("attach://"))
            self.assertTrue(photo.media.input_file_content)
        message.reply_photo.assert_not_awaited()
        message.reply_text.assert_not_awaited()

    async def test_aqicn_missing_keeps_nea_graph(self):
        message = await self.run_graphs(aqicn=[{"region": "North"}])
        args = message.reply_photo.call_args.kwargs
        self.assertEqual(args["photo"].getvalue(), b"nea png")
        self.assertIn("AQICN graph unavailable", args["caption"])
        message.reply_media_group.assert_not_awaited()
        message.reply_text.assert_not_awaited()

    async def test_nea_rate_limit_keeps_aqicn_graph(self):
        message = await self.run_graphs(
            nea_error=HTTPError("https://example.test", 429, "Too Many Requests", {}, None),
        )
        args = message.reply_photo.call_args.kwargs
        self.assertEqual(args["photo"].getvalue(), b"aqicn png")
        self.assertIn("NEA graph unavailable", args["caption"])

    async def test_nea_empty_readings_keeps_aqicn_graph(self):
        message = await self.run_graphs(nea=[{"PM2.5": {}, "PM10": {}}])
        self.assertIn("NEA graph unavailable", message.reply_photo.call_args.kwargs["caption"])

    async def test_nea_render_failure_keeps_aqicn_graph(self):
        message = await self.run_graphs(render_error=ValueError("render failed"))
        self.assertEqual(message.reply_photo.call_args.kwargs["photo"].getvalue(), b"aqicn png")

    async def test_both_sources_failing_send_retry_message(self):
        message = await self.run_graphs(
            nea_error=URLError("offline"), aqicn_error=TimeoutError("timed out"),
        )
        self.assertIn("NEA and AQICN", message.reply_text.call_args.args[0])
        self.assertIn("/graphs", message.reply_text.call_args.args[0])
        message.reply_media_group.assert_not_awaited()
        message.reply_photo.assert_not_awaited()

    async def test_both_sources_empty_send_retry_message(self):
        message = await self.run_graphs(nea=[], aqicn=[])
        message.reply_text.assert_awaited_once()
        message.reply_media_group.assert_not_awaited()
        message.reply_photo.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
