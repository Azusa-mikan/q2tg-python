from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


class TestRuntimeStats:
    @pytest.fixture(autouse=True)
    def clear_conversion_times(self):
        self._clear_conversion_times()
        yield
        self._clear_conversion_times()

    @staticmethod
    def _clear_conversion_times() -> None:
        for samples in conversion_times.values():
            samples.clear()

    def test_get_rss_scales_resident_memory(self) -> None:
        process = MagicMock()
        process.memory_info.return_value.rss = 2 * 1024**2
        with patch("src.runtime_stats.psutil.Process", return_value=process):
            assert _get_rss() == "2.00 MiB"

    def test_get_queue_sizes(self) -> None:
        from src.bus import message_bus
        from src.processing import media_processor

        with (
            patch.object(message_bus.onebot_queue, "qsize", return_value=1),
            patch.object(message_bus.onebot_event_queue, "qsize", return_value=2),
            patch.object(message_bus.onebot_system_queue, "qsize", return_value=3),
            patch.object(message_bus.telegram_queue, "qsize", return_value=4),
            patch.object(message_bus.telegram_event_queue, "qsize", return_value=5),
            patch.object(message_bus.telegram_system_queue, "qsize", return_value=6),
            patch.object(message_bus, "retry_queue_size", return_value=7),
            patch.object(media_processor.queue, "qsize", return_value=8),
        ):
            assert _get_queue_sizes() == QueueSizes(
                onebot_messages=1,
                onebot_events=2,
                onebot_system=3,
                telegram_messages=4,
                telegram_events=5,
                telegram_system=6,
                retry=7,
                media_processing=8,
            )

    @pytest.mark.asyncio
    async def test_track_conversion_keeps_latest_thirty_successes(self) -> None:
        operation = AsyncMock(return_value="result")
        with patch(
            "src.runtime_stats.perf_counter",
            side_effect=[value for index in range(31) for value in (index, index + 0.5)],
        ):
            for _ in range(31):
                assert await track_conversion("video", operation()) == "result"

        assert list(conversion_times["video"]) == [0.5] * 30
        assert _get_conversion_averages() == ConversionAverages(
            voice=None,
            video=0.5,
            sticker_static=None,
            sticker_tgs=None,
            sticker_video=None,
        )

    @pytest.mark.asyncio
    async def test_track_conversion_does_not_record_failures(self) -> None:
        async def fail() -> None:
            raise RuntimeError("failed")

        with pytest.raises(RuntimeError, match="failed"):
            await track_conversion("video", fail())

        assert list(conversion_times["video"]) == []

    def test_get_runtime_info_returns_complete_snapshot(self) -> None:
        queues = QueueSizes(
            onebot_messages=1,
            onebot_events=2,
            onebot_system=3,
            telegram_messages=4,
            telegram_events=5,
            telegram_system=6,
            retry=7,
            media_processing=8,
        )
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

        assert info.rss == "12.34 MiB"
        assert info.queues == queues
        assert info.conversion_averages == averages
