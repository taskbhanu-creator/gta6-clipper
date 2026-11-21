import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import gta6_clipper as clipper


class RunAutoTests(unittest.TestCase):
    def setUp(self):
        state_loader = patch.object(clipper, "_load_processed_video_ids", return_value=[])
        self.load_processed_ids = state_loader.start()
        self.addCleanup(state_loader.stop)
        state_writer = patch.object(clipper, "_mark_video_processed")
        self.mark_processed = state_writer.start()
        self.addCleanup(state_writer.stop)
        recorded_search = patch.object(clipper, "find_recorded_videos", return_value=[])
        self.find_recorded_videos = recorded_search.start()
        self.addCleanup(recorded_search.stop)
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
        "record_video_sample",
        side_effect=[clipper.StreamResolutionError("blocked"), Path("sample.mp4")],
    )
    @patch.object(clipper, "find_live_streams")
    def test_tries_next_stream_after_resolution_failure(
        self, find_live_streams, record_video_sample, *_mocks
    ):
        find_live_streams.return_value = self.streams

        clipper.run_auto()

        self.assertEqual(
            record_video_sample.call_args_list,
            [
                call(self.streams[0]["url"], duration_sec=180, start_sec=0),
                call(self.streams[1]["url"], duration_sec=180, start_sec=0),
            ],
        )

    def test_uses_recorded_video_when_no_live_streams_are_available(self):
        recorded_video = {
            "video_id": "recorded-id",
            "url": "https://example.com/recorded",
            "title": "GTA 6 gameplay",
            "streamer": "First",
            "handle": "@first",
            "start_sec": 420,
            "source": "recorded",
        }
        self.find_recorded_videos.return_value = [recorded_video]

        with (
            patch.object(clipper, "find_live_streams", return_value=[]) as find_live_streams,
            patch.object(clipper, "record_video_sample", return_value=Path("sample.mp4")) as record,
            patch.object(clipper, "detect_loudest_window", return_value=0.0),
            patch.object(clipper, "extract_clip", return_value=Path("raw.mp4")),
            patch.object(clipper, "render_vertical_short", return_value=Path("ready.mp4")),
            patch.object(clipper, "publish_youtube_short", return_value="yt-id"),
            patch.object(clipper, "publish_instagram_reel", return_value=""),
        ):
            clipper.run_auto()

        find_live_streams.assert_called_once_with(clipper.WHITELIST_CHANNELS)
        self.find_recorded_videos.assert_called_once_with(
            clipper.RECORDED_CHANNELS, set()
        )
        record.assert_called_once_with(
            recorded_video["url"], duration_sec=180, start_sec=420
        )
        self.mark_processed.assert_called_once_with("recorded-id")

    @patch.object(
        clipper,
        "record_video_sample",
        side_effect=clipper.StreamResolutionError("not a bot"),
    )
    @patch.object(clipper, "find_live_streams")
    def test_reports_cookie_guidance_when_all_streams_are_blocked(
        self, find_live_streams, _record_video_sample
    ):
        find_live_streams.return_value = self.streams

        with self.assertRaisesRegex(RuntimeError, "fresh, valid Netscape-format cookies.txt"):
            clipper.run_auto()

    @patch.object(
        clipper,
        "record_video_sample",
        side_effect=RuntimeError("ffmpeg capture failed"),
    )
    @patch.object(clipper, "find_live_streams")
    def test_does_not_skip_non_resolution_capture_errors(
        self, find_live_streams, record_video_sample
    ):
        find_live_streams.return_value = self.streams

        with self.assertRaisesRegex(RuntimeError, "ffmpeg capture failed"):
            clipper.run_auto()

        record_video_sample.assert_called_once()


class LoudestWindowTests(unittest.TestCase):
    def test_ignores_unavailable_rms_level(self):
        ffmpeg_output = (
            "frame:0 pts:0 pts_time:0.0\n"
            "RMS_level=-\n"
            "frame:2 pts:43 pts_time:43.0\n"
            "RMS_level=-18.5\n"
        )
        completed = subprocess.CompletedProcess([], 0, stdout=ffmpeg_output, stderr="")

        with patch.object(clipper.subprocess, "run", return_value=completed):
            peak_start = clipper.detect_loudest_window(Path("sample.mp4"))

        self.assertEqual(peak_start, 23.0)


class RecordVideoSampleTests(unittest.TestCase):
    def test_uses_default_player_clients_and_requested_sample_offset(self):
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
                clipper.record_video_sample("https://example.com/video", start_sec=37)

        self.assertNotIn("--extractor-args", commands[0])
        self.assertEqual(commands[0][commands[0].index("--js-runtimes") + 1], "node")
        self.assertEqual(commands[1][commands[1].index("-ss") + 1], "37")


class RecordedVideoDiscoveryTests(unittest.TestCase):
    def test_finds_recent_unprocessed_recordings_and_chooses_stable_sample_offset(self):
        channel = {
            "name": "First",
            "channel_id": "UCexample",
            "handle": "@first",
        }
        youtube = Mock()
        youtube.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "UCexample",
                    "contentDetails": {"relatedPlaylists": {"uploads": "uploads-id"}},
                }
            ]
        }
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {
                        "resourceId": {"videoId": "processed-id"},
                        "title": "GTA 6 old upload",
                        "description": "",
                        "publishedAt": "2026-11-20T10:00:00Z",
                    }
                },
                {
                    "snippet": {
                        "resourceId": {"videoId": "recorded-id"},
                        "title": "GTA 6 gameplay",
                        "description": "",
                        "publishedAt": "2026-11-21T10:00:00Z",
                    }
                },
            ]
        }
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "recorded-id",
                    "snippet": {"liveBroadcastContent": "none"},
                    "contentDetails": {"duration": "PT2H5M"},
                }
            ]
        }

        with patch.object(clipper, "YOUTUBE_API_KEY", "test-key"), patch.object(
            clipper, "build", return_value=youtube
        ):
            videos = clipper.find_recorded_videos([channel], {"processed-id"})

        self.assertEqual([video["video_id"] for video in videos], ["recorded-id"])
        self.assertEqual(videos[0]["source"], "recorded")
        self.assertGreaterEqual(videos[0]["start_sec"], 0)
        self.assertLessEqual(videos[0]["start_sec"], 7500 - 180)
        self.assertEqual(
            videos[0]["start_sec"], clipper._sample_start("recorded-id", 7500)
        )


class ProcessedVideoStateTests(unittest.TestCase):
    def test_persists_processed_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "processed_videos.json"
            with patch.object(clipper, "PROCESSED_VIDEOS_FILE", state_path):
                clipper._mark_video_processed("video-1")

                self.assertEqual(clipper._load_processed_video_ids(), ["video-1"])


if __name__ == "__main__":
    unittest.main()
