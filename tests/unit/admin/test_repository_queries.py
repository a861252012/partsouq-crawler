from partsouq_crawler.admin.query_trace import QueryTrace
from partsouq_crawler.admin.repository import (
    FANOUT_LIMIT,
    AdminDataError,
    AdminRepository,
)

from .fakes import ScriptedDatabase


def _list_query_shape(dataset_size: int) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    trace = QueryTrace()
    repository = AdminRepository(ScriptedDatabase(trace, dataset_size=dataset_size))

    repository.list_records("part_numbers", limit=25)

    return trace.count, trace.tags, trace.fingerprints


def test_list_query_shape_is_fixed_for_one_hundred_and_ten_thousand_rows() -> None:
    one = _list_query_shape(1)
    hundred = _list_query_shape(100)
    ten_thousand = _list_query_shape(10_000)

    assert one[0] == hundred[0] == ten_thousand[0] == 3
    assert one[1] == hundred[1] == ten_thousand[1]
    assert one[2] == hundred[2] == ten_thousand[2]


def _detail_query_shape(fanout: int) -> tuple[int, tuple[str, ...], tuple[str, ...], int, int]:
    trace = QueryTrace()
    database = ScriptedDatabase(
        trace,
        dataset_size=1,
        event_count=min(fanout, FANOUT_LIMIT + 1),
        provenance_count=min(fanout, FANOUT_LIMIT + 1),
    )
    detail = AdminRepository(database).get_record("part_numbers", "source:1")
    return (
        trace.count,
        trace.tags,
        trace.fingerprints,
        len(detail.events),
        len(detail.provenance),
    )


def test_detail_query_shape_is_fixed_and_high_fanout_is_bounded() -> None:
    one = _detail_query_shape(1)
    high_fanout = _detail_query_shape(10_000)

    assert one[:3] == high_fanout[:3]
    assert one[0] == 4
    assert high_fanout[3:] == (FANOUT_LIMIT, FANOUT_LIMIT)


def test_source_update_only_writes_overlay_and_event_tables() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)
    repository = AdminRepository(database)

    revision = repository.update_record(
        "part_numbers",
        "source:1",
        {"name_en_raw": "人工校正名稱"},
        expected_revision=0,
        actor="tester",
        reason="比對原廠型錄",
    )

    assert revision == 1
    write_sql = "\n".join(
        call.sql.lower() for call in database.calls if call.tag.startswith("write.")
    )
    assert "insert into admin_override_heads" in write_sql
    assert "insert into admin_override_events" in write_sql
    assert "update part_numbers" not in write_sql
    assert "delete" not in write_sql


def test_entity_allowlist_rejects_sql_identifier_injection_before_query() -> None:
    trace = QueryTrace()
    repository = AdminRepository(ScriptedDatabase(trace))

    try:
        repository.list_records("part_numbers; DROP TABLE part_numbers")
    except AdminDataError:
        pass
    else:
        raise AssertionError("invalid entity type was accepted")

    assert trace.count == 0


def test_source_search_uses_index_friendly_prefix_while_overlay_is_substring() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    AdminRepository(database).list_records("part_numbers", query="ABC", limit=25)

    params = database.calls[0].params
    assert params is not None
    values = list(params)
    assert values[:3] == ["ABC%"] * 3
    assert values[9:12] == ["ABC%"] * 3
    assert values[16] == "%ABC%"
    assert " UNION " in database.calls[0].sql


def test_admin_views_redact_sensitive_source_urls() -> None:
    trace = QueryTrace()
    repository = AdminRepository(ScriptedDatabase(trace, provenance_count=1))

    page = repository.list_records("part_numbers", limit=25)
    detail = repository.get_record("part_numbers", "source:1")

    assert page.records[0].payload["source_url"].endswith("ssd=[REDACTED]")
    assert detail.record.source_payload is not None
    assert detail.record.source_payload["source_url"].endswith("ssd=[REDACTED]")
    assert detail.provenance[0]["source_url"].endswith("ssd=[REDACTED]")


def test_crawl_monitoring_query_shape_is_fixed() -> None:
    trace = QueryTrace()
    monitor = AdminRepository(ScriptedDatabase(trace)).crawl_monitoring()

    assert trace.count == 3
    assert trace.tags == (
        "monitor.monthly-runs",
        "monitor.crawl-runs",
        "monitor.events",
    )
    assert monitor["monthly_runs"][0]["period_key"] == "2099-01"
    assert monitor["crawl_runs"][0]["responses"] == 10
