"""11 masked test queries for ai/ development (masked text only).

Generated once from db/queries.py by masking outside ai/: table names ->
t_xxx, columns -> c_xxx, output names -> x_xxx, table aliases -> a1..,
literal values -> placeholders. Odd entries use numbered $1, $2 ...;
even entries use bare ?, so code is tested on both styles.
"""

MASKED_QUERIES = {
    'q01': (
        'SELECT c_ca1, c_02b, c_cf3, c_073 FROM t_1c1 WHERE c_1e3 = ? ORDER BY c_02b DESC'
    ),
    'q02': (
        'SELECT a3.c_82a AS x_c69, COUNT(*) AS x_1c1, SUM(a1.c_cf3) AS x_371 FROM t_1c1 AS a1 JOIN t_21e AS a2 ON a2.c_1e3 = a1.c_1e3 JOIN t_f23 AS a3 ON a3.c_0b8 = a2.c_0b8 WHERE a1.c_02b BETWEEN $1 AND $2 GROUP BY a3.c_82a ORDER BY x_371 DESC'
    ),
    'q03': (
        'SELECT c_ca1, c_1e3, c_cf3 FROM t_1c1 WHERE c_073 = ? AND c_02b >= ?'
    ),
    'q04': (
        'SELECT a1.c_82a, (SELECT SUM(a2.c_cf3) FROM t_1c1 AS a2 WHERE a2.c_1e3 = a1.c_1e3) AS x_a81 FROM t_21e AS a1 WHERE a1.c_03e = $1 AND a1.c_1e3 <= $2'
    ),
    'q05': (
        "SELECT COUNT(*), SUM(c_cf3) FROM t_1c1 WHERE DATE_TRUNC('MONTH', c_02b) = ?"
    ),
    'q06': (
        'SELECT a2.c_82a, SUM(a1.c_cf3) AS x_371 FROM t_1c1 AS a1 JOIN t_0a3 AS a2 ON a2.c_c3a = a1.c_c3a WHERE a2.c_edb = $2 AND a1.c_02b >= $3 GROUP BY a2.c_82a ORDER BY x_371 DESC LIMIT $1'
    ),
    'q07': (
        'SELECT c_1e3, c_82a, c_822 FROM t_21e WHERE NOT c_1e3 IN (SELECT c_1e3 FROM t_1c1)'
    ),
    'q08': (
        "SELECT DATE_TRUNC('MONTH', c_02b) AS x_a5c, SUM(c_cf3) FROM t_1c1 WHERE c_02b >= $1 AND c_02b < $2 GROUP BY 1 ORDER BY 1"
    ),
    'q09': (
        'SELECT c_ca1, c_1e3, c_c3a FROM t_1c1 WHERE c_1e3 = ? OR c_c3a = ?'
    ),
    'q10': (
        'SELECT COUNT(DISTINCT c_1e3) FROM t_1c1 WHERE c_c3a = $1 AND c_073 = $2'
    ),
    'q11': (
        "SELECT a2.c_82a AS x_c69, DATE_TRUNC('WEEK', a1.c_02b) AS x_6df, a3.c_edb, COUNT(DISTINCT a1.c_1e3) AS x_274, SUM(a1.c_cf3) AS x_371 FROM t_1c1 AS a1 JOIN t_f23 AS a2 ON a2.c_0b8 = a1.c_0b8 JOIN t_0a3 AS a3 ON a3.c_c3a = a1.c_c3a JOIN t_21e AS a4 ON a4.c_1e3 = a1.c_1e3 WHERE a1.c_0b8 = ? AND a1.c_02b >= ? AND a4.c_03e <> ? GROUP BY a2.c_82a, x_6df, a3.c_edb ORDER BY x_6df, x_371 DESC"
    ),
}
