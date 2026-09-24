"""
Tests del análisis de loudness sin recodificar (``radio.producers.post``):
``FfmpegLoudnessAnalyzer`` con un ``runner`` falso (sin ffmpeg), interpretación de la
salida de ``ebur128`` y ``loudnorm``, ``analyze_stock`` y ``radio analyze-loudness``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from radio.cli import app
from radio.core.models import Segment
from radio.core.store import DB
from radio.producers.post import (
    AudioAnalyzer,
    FfmpegLoudnessAnalyzer,
    LoudnessMeasurement,
    NullAnalyzer,
    PostError,
    analyze_stock,
    choose_analyzer,
    parse_ebur128_summary,
    parse_loudness,
    parse_loudnorm_summary,
)

REPO = Path(__file__).parents[2]

# Salida real (recortada) de ffmpeg 6 con ``-af ebur128=peak=true:framelog=verbose``
EBUR128_STDERR = """\
Input #0, mp3, from 'concierto.mp3':
  Metadata:
    title           : Tiny Desk Concert
  Duration: 00:18:41.02, start: 0.025057, bitrate: 128 kb/s
  Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 128 kb/s
Stream mapping:
  Stream #0:0 -> #0:0 (mp3 (mp3float) -> pcm_s16le (native))
Output #0, null, to 'pipe:':
  Stream #0:0: Audio: pcm_s16le, 48000 Hz, stereo, s16, 1536 kb/s
[Parsed_ebur128_0 @ 0x5581d6c0a0c0] Summary:

  Integrated loudness:
    I:         -19.3 LUFS
    Threshold: -29.6 LUFS

  Loudness range:
    LRA:         6.1 LU
    Threshold: -39.6 LUFS
    LRA low:   -23.5 LUFS
    LRA high:  -17.4 LUFS

  True peak:
    Peak:       -0.6 dBFS
"""

# Con líneas por trama (framelog=info) delante: solo cuenta el resumen final
EBUR128_WITH_FRAMES = (
    "[Parsed_ebur128_0 @ 0x1] t: 0.1  TARGET:-23 LUFS    M: -120.7 S: -120.7"
    "     I: -70.0 LUFS       LRA:   0.0 LU  FTPK: -inf dBFS  TPK: -inf dBFS\n"
    "[Parsed_ebur128_0 @ 0x1] t: 0.2  TARGET:-23 LUFS    M:  -25.0 S: -120.7"
    "     I: -25.0 LUFS       LRA:   0.0 LU  FTPK: -3.0 dBFS  TPK: -3.0 dBFS\n"
    + EBUR128_STDERR
)

EBUR128_NO_PEAK = """\
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.2 LUFS
    Threshold: -24.5 LUFS

  Loudness range:
    LRA:         3.0 LU
"""

EBUR128_SILENCE = """\
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -70.0 LUFS
    Threshold:   0.0 LUFS

  True peak:
    Peak:       -inf dBFS
"""

LOUDNORM_STDERR = """\
[Parsed_loudnorm_0 @ 0x55]
{
\t"input_i" : "-21.07",
\t"input_tp" : "-3.40",
\t"input_lra" : "7.20",
\t"input_thresh" : "-31.50",
\t"output_i" : "-16.02",
\t"output_tp" : "-1.50",
\t"output_lra" : "6.00",
\t"output_thresh" : "-26.50",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.02"
}
"""

LOUDNORM_SILENCE = LOUDNORM_STDERR.replace('"-21.07"', '"-inf"').replace('"-3.40"', '"-inf"')


# ── Interpretación ────────────────────────────────────────────────────────────

def test_parse_ebur128_summary() -> None:
    assert parse_ebur128_summary(EBUR128_STDERR) == LoudnessMeasurement(-19.3, -0.6)


def test_parse_ebur128_ignores_frame_lines() -> None:
    assert parse_ebur128_summary(EBUR128_WITH_FRAMES) == LoudnessMeasurement(-19.3, -0.6)


def test_parse_ebur128_without_true_peak() -> None:
    assert parse_ebur128_summary(EBUR128_NO_PEAK) == LoudnessMeasurement(-14.2, None)


@pytest.mark.parametrize(
    "stderr", ["", "Input #0 ... sin resumen", EBUR128_SILENCE,
               "[Parsed_ebur128_0] Summary:\n  Loudness range:\n    LRA: 1.0 LU\n"],
    ids=["vacío", "sin-resumen", "silencio", "sin-I"],
)
def test_parse_ebur128_errors(stderr: str) -> None:
    with pytest.raises(PostError):
        parse_ebur128_summary(stderr)


def test_parse_loudnorm_summary() -> None:
    assert parse_loudnorm_summary(LOUDNORM_STDERR) == LoudnessMeasurement(-21.07, -3.4)
    with pytest.raises(PostError, match="no medible"):
        parse_loudnorm_summary(LOUDNORM_SILENCE)


def test_parse_loudness_detects_format() -> None:
    assert parse_loudness(EBUR128_STDERR).integrated_lufs == -19.3
    assert parse_loudness(LOUDNORM_STDERR).integrated_lufs == -21.07
    with pytest.raises(PostError):
        parse_loudness("nada")


# ── Analizador con runner falso ───────────────────────────────────────────────

def test_ffmpeg_analyzer_single_read_only_pass(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> tuple[int, str]:
        calls.append(list(args))
        return 0, EBUR128_STDERR

    audio = tmp_path / "concierto.mp3"
    audio.write_bytes(b"ID3 datos")
    analyzer = FfmpegLoudnessAnalyzer(ffmpeg="/usr/bin/ffmpeg", runner=runner)
    assert isinstance(analyzer, AudioAnalyzer)
    assert analyzer.analyze(audio) == LoudnessMeasurement(-19.3, -0.6)

    [args] = calls                                  # una sola pasada
    assert args[0] == "/usr/bin/ffmpeg"
    assert args[args.index("-i") + 1] == str(audio)
    assert args[args.index("-af") + 1] == "ebur128=peak=true:framelog=verbose"
    assert args[-3:] == ["-f", "null", "-"]         # no escribe ningún audio
    assert "-nostats" in args and "-y" not in args
    assert audio.read_bytes() == b"ID3 datos"       # el archivo no se toca


def test_ffmpeg_analyzer_loudnorm_method() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> tuple[int, str]:
        calls.append(list(args))
        return 0, LOUDNORM_STDERR

    analyzer = FfmpegLoudnessAnalyzer(method="loudnorm", runner=runner)
    assert analyzer.analyze(Path("x.mp3")) == LoudnessMeasurement(-21.07, -3.4)
    assert "print_format=json" in calls[0][calls[0].index("-af") + 1]
    assert calls[0][-3:] == ["-f", "null", "-"]


def test_ffmpeg_analyzer_failure_raises() -> None:
    with pytest.raises(PostError, match="análisis"):
        FfmpegLoudnessAnalyzer(runner=lambda a: (1, "Invalid data")).analyze(Path("x.mp3"))


def test_null_analyzer_and_choose() -> None:
    assert NullAnalyzer().analyze(Path("x")) is None
    assert isinstance(choose_analyzer(which=lambda _n: None), NullAnalyzer)
    chosen = choose_analyzer(which=lambda _n: "/opt/ffmpeg")
    assert isinstance(chosen, FfmpegLoudnessAnalyzer) and chosen.ffmpeg == "/opt/ffmpeg"


# ── analyze_stock / radio analyze-loudness ────────────────────────────────────

def music(seg_id: str, path: Path, **meta: object) -> Segment:
    return Segment(
        id=seg_id, kind="music", factual=False, path=path, duration_s=60.0,
        created_at=datetime(2026, 9, 1, tzinfo=UTC), producer="music_tinydesk",
        meta={"title": seg_id, **meta},
    )


class DictAnalyzer:
    def __init__(self, values: dict[str, LoudnessMeasurement | Exception]) -> None:
        self.values = values
        self.seen: list[str] = []

    def analyze(self, path: Path) -> LoudnessMeasurement:
        self.seen.append(path.name)
        value = self.values[path.name]
        if isinstance(value, Exception):
            raise value
        return value


def test_analyze_stock_updates_meta(tmp_path: Path) -> None:
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (tmp_path / name).write_bytes(b"x")
    db = DB(":memory:")
    db.add_segment(music("a", tmp_path / "a.mp3", guid="g-a"))
    db.add_segment(music("b", tmp_path / "b.mp3", loudness_lufs=-18.0, true_peak_db=-1.0))
    db.add_segment(music("c", tmp_path / "c.mp3"))
    db.add_segment(music("gone", tmp_path / "gone.mp3"))
    analyzer = DictAnalyzer({
        "a.mp3": LoudnessMeasurement(-20.123, -2.456),
        "b.mp3": LoudnessMeasurement(-10.0, 0.0),
        "c.mp3": PostError("ffmpeg salió con 1"),
    })

    report = analyze_stock(db, analyzer, missing_only=True)
    assert (report.measured, report.skipped, len(report.failed)) == (1, 2, 1)
    assert analyzer.seen == ["a.mp3", "c.mp3"]      # b ya tenía medida; gone no existe
    a = db.get_segment("a")
    assert a is not None
    assert a.meta["loudness_lufs"] == -20.12 and a.meta["true_peak_db"] == -2.46
    assert a.meta["guid"] == "g-a"                  # el resto de meta se conserva

    report = analyze_stock(db, analyzer, missing_only=False)
    assert report.measured == 2                     # a y b (c sigue fallando)
    b = db.get_segment("b")
    assert b is not None and b.meta["loudness_lufs"] == -10.0


def test_update_segment_meta_unknown_id() -> None:
    with pytest.raises(KeyError):
        DB(":memory:").update_segment_meta("nope", {})


def test_cli_analyze_loudness_without_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with DB(tmp_path / "state.db") as db:
        db.add_segment(music("a", tmp_path / "a.mp3"))
    monkeypatch.setattr("radio.producers.post.shutil.which", lambda _n: None)
    result = CliRunner().invoke(app, [
        "analyze-loudness", "--config-dir", str(REPO / "config"), "--data-dir", str(tmp_path),
    ])
    assert result.exit_code == 1
    assert "ffmpeg" in result.output


def test_cli_analyze_loudness_with_fake_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.mp3").write_bytes(b"x")
    with DB(tmp_path / "state.db") as db:
        db.add_segment(music("a", tmp_path / "a.mp3"))
    monkeypatch.setattr("radio.producers.post.shutil.which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("radio.producers.post._run_subprocess",
                        lambda _args: (0, EBUR128_STDERR))
    result = CliRunner().invoke(app, [
        "analyze-loudness", "--missing-only",
        "--config-dir", str(REPO / "config"), "--data-dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert "Medidos: 1" in result.output
    with DB(tmp_path / "state.db") as db:
        seg = db.get_segment("a")
    assert seg is not None and seg.meta["loudness_lufs"] == -19.3
