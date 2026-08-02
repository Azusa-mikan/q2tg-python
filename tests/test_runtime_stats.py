import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.runtime_stats import (
    ConversionAverages,
    QueueSizes,
    _get_conversion_averages,
    _get_queue_sizes,
    _get_rss,
    conversion_times,
    get_runtime_info,
    track_conversion,
)


class RuntimeStatsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._clear_conversion_times()

    def tearDown(self) -> None:
        self._clear_conversion_times()

    @staticmethod
    def _clear_conversion_times() -> None:
        conversion_times.voice.clear()
        conversion_times.video.clear()
        conversion_times.sticker_static.clear()
        conversion_times.sticker_tgs.clear()
        conversion_times.sticker_video.clear()

    def test_get_rss_scales_resident_memory(self) -> None:
        process = MagicMock()
        process.memory_info.return_value.rss = 2 * 1024**2
        with patch("src.runtime_stats.psutil.Process", return_value=process):
            self.assertEqual(_get_rss(), "2.00 MiB")

    def test_get_queue_sizes(self) -> None:
        from src.bus import message_bus
        from src.processing import media_processor

        with (
            patch.object(message_bus.onebot_queue, "qsize", return_value=1),
            patch.object(message_bus.telegram_queue, "qsize", return_value=2),
            patch.object(message_bus.retry_queue, "qsize", return_value=3),
            patch.object(media_processor.queue, "qsize", return_value=4),
        ):
            self.assertEqual(
                _get_queue_sizes(),
                QueueSizes(onebot=1, telegram=2, retry=3, media_processing=4),
            )

    async def test_track_conversion_keeps_latest_thirty_successes(self) -> None:
        operation = AsyncMock(return_value="result")
        with patch(
            "src.runtime_stats.perf_counter",
            side_effect=[value for index in range(31) for value in (index, index + 0.5)],
        ):
            for _ in range(31):
                self.assertEqual(await track_conversion("video", operation()), "result")

        self.assertEqual(list(conversion_times.video), [0.5] * 30)
        self.assertEqual(
            _get_conversion_averages(),
            ConversionAverages(
                voice=None,
                video=0.5,
                sticker_static=None,
                sticker_tgs=None,
                sticker_video=None,
            ),
        )

    async def test_track_conversion_does_not_record_failures(self) -> None:
        async def fail() -> None:
            raise RuntimeError("failed")

        with self.assertRaisesRegex(RuntimeError, "failed"):
            await track_conversion("video", fail())

        self.assertEqual(list(conversion_times.video), [])

    def test_get_runtime_info_returns_complete_snapshot(self) -> None:
        queues = QueueSizes(onebot=1, telegram=2, retry=3, media_processing=4)
        averages = ConversionAverages(
            voice=None,
            video=0.5,
            sticker_static=None,
            sticker_tgs=None,
            sticker_video=None,
        )
        with (
            patch("src.runtime_stats._get_rss", return_value="12.34 MiB"),
            patch("src.runtime_stats._get_queue_sizes", return_value=queues),
            patch("src.runtime_stats._get_conversion_averages", return_value=averages),
        ):
            info = get_runtime_info()

        self.assertEqual(info.rss, "12.34 MiB")
        self.assertEqual(info.queues, queues)
        self.assertEqual(info.conversion_averages, averages)
