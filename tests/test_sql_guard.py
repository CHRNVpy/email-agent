import pytest

from app.tools.sql import UnsafeQuery, check_read_only, check_write


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM orders WHERE id = :id",
        "  with recent as (select * from orders) select count(*) from recent;",
        "-- comment\nSHOW TABLES",
        "EXPLAIN SELECT 1",
    ],
)
def test_read_only_accepts_queries(query):
    assert check_read_only(query)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM orders",
        "SELECT 1; DROP TABLE orders",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "/* hidden */ UPDATE orders SET total = 0",
    ],
)
def test_read_only_rejects_writes(query):
    with pytest.raises(UnsafeQuery):
        check_read_only(query)


def test_write_requires_where_for_update_and_delete():
    assert check_write("UPDATE orders SET status = :s WHERE id = :id")
    assert check_write("INSERT INTO notes (body) VALUES (:body)")
    with pytest.raises(UnsafeQuery):
        check_write("DELETE FROM orders")
    with pytest.raises(UnsafeQuery):
        check_write("DROP TABLE orders")
