# Implementation decisions

1. **Indexes.** `employees.emp_code` is unique for the employee identity guarantee. `employees(department, joined_on)`
supports headcount lookups. `attendance_logs(emp_code, date)` is unique for the natural attendance key and punch-in race.
`attendance_logs(date, emp_code)` supports the sorted list and date-bounded analytics, while the open-punch index supports
the most recent open record lookup. I considered a standalone `status` index but rejected it because status is low
cardinality and the useful queries already lead with employee or date.

2. **Punch-in race.** Both requests resolve the employee and calculate the same natural key. MongoDB's unique
`(emp_code, date)` index allows one insert and rejects the other with `DuplicateKeyError`; the winner returns 201 and the
loser returns 409. There is no check-then-insert decision that can race.

3. **Ties.** The leaderboard uses MongoDB's standard competition rank after sorting by total late minutes. Filtering is
applied to `rank <= limit`, not to row position, so every employee tied at the cutoff is returned and the next rank is
skipped.

4. **Headcount.** The department summary starts from employees whose `joined_on` is within the month's headcount cutoff,
then performs a lookup of logs and unwinds with `preserveNullAndEmptyArrays`. The employee count is therefore retained
even when the lookup returns no documents.

5. **One thing I would change** if this had to serve 100x the data. I would move monthly aggregates to a scheduled
summary collection while retaining the raw attendance log as the source of audit truth. That would keep interactive
analytics predictable without recomputing large ranges for every request.
