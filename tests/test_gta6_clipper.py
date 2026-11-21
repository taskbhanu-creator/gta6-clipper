import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import gta6_clipper as clipper


class RunAutoTests(unittest.TestCase):
    def setUp(self):
        self.streams = [
            {
                "video_id": "first",
                "url": "https://example.com/first",
                "streamer": "First",
                "title": "First",
                "handle": "@first",
            },
            {
                "video_id": "second",
                "url": "https://example.com/second",
                "streamer": "Second",
                "title": "Second",
                "handle": "@second",
            },
        ]

    @patch.object(clipper, "publish_instagram_reel", return_value="ig-id")
    @patch.object(clipper, "publish_youtube_short", return_value="yt-id")
    @patch.object(clipper, "render_vertical_short", return_value=Path("ready.mp4"))
    @patch.object(clipper, "extract_clip", return_value=Path("raw.mp4"))
    @patch.object(clipper, "detect_loudest_window", return_value=0.0)
    @patch.object(
        clipper,
        "record_live_sample",
        side_effect=[clipper.StreamResolutionError("blocked"), Path("sample.mp4")],
    )
    @patch.object(clipper, "find_live_streams")
    def test_tries_next_stream_after_resolution_failure(
        self, find_live_streams, record_live_sample, *_mocks
    ):
        find_live_streams.return_value = self.streams

        clipper.run_auto()

        self.assertEqual(
            record_live_sample.call_args_list,
            [
                call(self.streams[0]["url"], duration_sec=180),
                call(self.streams[1]["url"], duration_sec=180),
            ],
        )

    @patch.object(
        clipper,
        "record_live_sample",
        side_effect=clipper.StreamResolutionError("not a bot"),
    )
    @patch.object(clipper, "find_live_streams")
    def test_reports_cookie_guidance_when_all_streams_are_blocked(
        self, find_live_streams, _record_live_sample
    ):
        find_live_streams.return_value = self.streams

        with self.assertRaisesRegex(RuntimeError, "fresh, valid Netscape-format cookies.txt"):
            clipper.run_auto()

    @patch.object(
        clipper,
        "record_live_sample",
        side_effect=RuntimeError("ffmpeg capture failed"),
    )
    @patch.object(clipper, "find_live_streams")
    def test_does_not_skip_non_resolution_capture_errors(
        self, find_live_streams, record_live_sample
    ):
        find_live_streams.return_value = self.streams

        with self.assertRaisesRegex(RuntimeError, "ffmpeg capture failed"):
            clipper.run_auto()

        record_live_sample.assert_called_once()


class RecordLiveSampleTests(unittest.TestCase):
    def test_uses_ytdlp_default_player_clients(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            if command[0] == "yt-dlp":
                return subprocess.CompletedProcess(command, 0, stdout="https://example.com/stream", stderr="")
            Path(command[-1]).write_bytes(b"sample")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with patch.multiple(
                clipper,
                WORK_DIR=work_dir,
                CHUNKS_DIR=work_dir / "chunks",
                CLIPS_DIR=work_dir / "clips",
                READY_DIR=work_dir / "ready",
            ), patch.object(clipper, "_cookies_args", return_value=[]), patch.object(
                clipper.subprocess, "run", side_effect=fake_run
            ):
                clipper.record_live_sample("https://example.com/video")

        self.assertNotIn("--extractor-args", commands[0])


if __name__ == "__main__":
    unittest.main()
