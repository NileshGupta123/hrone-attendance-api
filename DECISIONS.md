# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)`, `joined_on` (headcount filter). `attendance_logs`: unique `(emp_code, date)` (one record per day, settles races), `(date desc, emp_code)` (default list order, month scans), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (find the record to punch out). Rejected: a plain `emp_code` index, because the unique compound already covers it.

2. **Punch-in race.** Both requests go straight to `insert_one`, with no check first. The unique `(emp_code, date)` index lets MongoDB accept one insert; the other raises `DuplicateKeyError`, which I return as 409. One 201, one 409, never two records.

3. **Ties.** Everyone tied at the cutoff is returned. I rank first (`$rank`: 1, 2, 2, 4), then keep `rank <= limit`, so the list can be longer than `limit`. Cutting between equal scores would be arbitrary.

4. **Headcount.** The pipeline starts from `employees`, filtered by `joined_on <= month end`. Logs are joined with `$lookup`, and `$unwind` keeps employees that have none.

5. **At 100x.** Keep running monthly totals per employee, updated with `$inc` on each write, so analytics read small documents instead of scanning every log.