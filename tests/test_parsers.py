from datetime import UTC, datetime, timedelta, timezone

from conftest import write_jsonl

from agentlog.parsers import Excerpt, clip, parse_ts, read_jsonl


def test_read_jsonl_skips_blank_garbage_partial_and_non_object_lines(tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", [{"a": 1}, "", "garbage", "[1, 2]", "42", {"b": 2},
                                              '{"partial": '])
    assert list(read_jsonl(path)) == [{"a": 1}, {"b": 2}]


def test_parse_ts_reads_iso_with_z_suffix_as_utc():
    assert parse_ts("2026-01-02T10:00:00.500Z") == datetime(2026, 1, 2, 10, 0, 0, 500000, UTC)


def test_parse_ts_keeps_explicit_offset():
    dt = parse_ts("2026-01-02T12:00:00+02:00")
    assert dt.utcoffset() == timedelta(hours=2)
    assert dt == datetime(2026, 1, 2, 10, tzinfo=UTC)


def test_parse_ts_treats_naive_iso_as_utc():
    assert parse_ts("2026-01-02T10:00:00").tzinfo == UTC


def test_parse_ts_reads_unix_seconds_int_and_float():
    assert parse_ts(1767348000) == datetime(2026, 1, 2, 10, tzinfo=UTC)
    assert parse_ts(1767348000.25) == datetime(2026, 1, 2, 10, 0, 0, 250000, UTC)


def test_parse_ts_returns_none_for_invalid_or_missing_values():
    assert parse_ts("yesterday") is None
    assert parse_ts(None) is None
    assert parse_ts({"t": 1}) is None


def test_parse_ts_result_is_always_aware():
    for v in ("2026-01-02T10:00:00", "2026-01-02T10:00:00Z", 0):
        assert parse_ts(v).tzinfo is not None
    assert parse_ts("2026-01-02T10:00:00-05:00").tzinfo == timezone(timedelta(hours=-5))


def test_clip_leaves_short_text_untouched():
    assert clip("short", 10) == "short"
    assert clip("x" * 10, 10) == "x" * 10


def test_clip_keeps_head_and_tail_and_reports_cut_size():
    text = "H" * 500 + "M" * 1000 + "T" * 500
    out = clip(text, 200)
    assert out.startswith("H" * 80)
    assert out.endswith("T" * 80)
    assert "M" not in out
    assert f"[{2000 - 160} chars cut]" in out
    assert len(out) < 250


def test_excerpt_labels_lines_skips_blank_and_clips_each_piece():
    ex = Excerpt()
    ex.add("user", "  hello  ")
    ex.add("assistant", "   ")
    ex.add("result", "r" * 5000, limit=100)
    lines = ex.render(100_000).split("\n", 1)
    assert lines[0] == "[user] hello"
    assert lines[1].startswith("[result] ")
    assert "chars cut" in lines[1]
    assert len(lines[1]) < 200


def test_excerpt_render_caps_total_size():
    ex = Excerpt()
    for i in range(100):
        ex.add("assistant", f"message {i} " + "z" * 500)
    out = ex.render(2000)
    assert len(out) < 2100
    assert out.startswith("[assistant] message 0 ")
    assert out.rstrip().endswith("z")


def test_clip_with_tiny_limit_still_truncates():
    from agentlog.parsers import clip

    assert clip("x" * 1000, 30) == "x" * 30
