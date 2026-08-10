from partsouq_crawler.admin.query_trace import QueryTrace, sql_fingerprint


def test_sql_fingerprint_ignores_query_tag_and_whitespace() -> None:
    first = "/* admin:list.keys */ SELECT id\n FROM part_numbers WHERE id = %s"
    second = "/* admin:list.keys.retry */  select id from part_numbers where id = %s"

    assert sql_fingerprint(first) == sql_fingerprint(second)


def test_query_trace_keeps_tag_count_and_fingerprint() -> None:
    trace = QueryTrace()
    trace.record(tag="detail.base", sql="SELECT 1", elapsed_ms=0.5, row_count=1)

    assert trace.count == 1
    assert trace.tags == ("detail.base",)
    assert trace.fingerprints == ("select 1",)
