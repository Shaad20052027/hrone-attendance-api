# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

1. **Indexes.** On employees: unique `emp_code` (no duplicates, fast lookups), `(department, emp_code)` for the sorted list, and `joined_on` / `(department, joined_on)` for headcount. On attendance_logs: unique `(emp_code, date)` (one record per day, and it protects against double punch-ins), `(date desc, emp_code)` for the sorted list and leaderboard, `(status, date, emp_code)` for status filters, and `(emp_code, punch_in desc)` for punch-out. I rejected an index on `status` alone because it has very few distinct values and the compound one covers it.

2. **Punch-in race.** Both requests reach `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed. The other gets `DuplicateKeyError`, which I turn into a 409. There is no check-then-insert.

3. **Ties.** I rank with `$rank` (1, 2, 2, 4), then keep every row with rank <= limit. So all tied employees at the cutoff are returned, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees`, joins the logs with `$lookup`, and uses `$unwind` with `preserveNullAndEmptyArrays`, so an employee with no logs still produces a row and is counted.

5. **100x data.** The trend passes the department's `emp_code` list into `$in`. I would store the department on each attendance record and index `(department, date)`, or pre-aggregate daily numbers into a summary collection.
