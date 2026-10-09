# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Used naive local datetimes, so UTC server settings changed the result and the grace rule was not consistently represented. | A punch supplied as epoch milliseconds produced different dates/results on a non-IST host; a punch exactly 10 minutes late needed an explicit boundary test. | All instants are converted to UTC/IST at the API boundary and late minutes use the strict greater-than-10-minute rule. |
| 2 | `compute_work_hours` | Python `round` uses banker's rounding rather than the required half-up rounding. | A duration whose third decimal is exactly 5 returned the wrong two-decimal value. | Duration calculations use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | It used the record date as a same-day shift end, which is wrong for overnight shifts, and did not enforce the 30-minute threshold. | The sample 22:00-06:00 shift and a 29-minute extension exposed incorrect overtime. | Shift end is placed on the following IST day for overnight shifts and overtime is stored only at 30 minutes or more. |
| 4 | `create_employee` | Duplicate prevention was a check followed by insert, which allowed concurrent duplicates; it also returned a BSON internal timestamp shape rather than epoch milliseconds. | Concurrent POSTs or inspecting `created_at` in the response showed the issue. | A unique database index handles the race and timestamps are converted at the API boundary. |
| 5 | `list_employees` | Pagination skipped `page * page_size` rows and `total` ignored the department filter. | Page 1 omitted records and filtered totals were too large. | Pagination uses `(page - 1) * page_size` and count uses the same filter. |
| 6 | `punch_in` | Unknown employees caused a server error, timestamps used host local time, and check-then-insert was race-prone. | An unknown code raised a `TypeError`; simultaneous requests could create two records. | Employee existence is checked explicitly, timestamps are normalized to IST/UTC rules, and the unique attendance key makes insertion atomic. |
| 7 | `list_attendance` | It loaded and sorted the entire collection in Python, and it did not validate date ordering. | Large collections became slow and date filters with `date_from > date_to` silently returned an empty page. | MongoDB performs filtering, sorting, skip, and limit; invalid ranges return 422. |
| 8 | Missing contract endpoints | Punch-out, regularization, all analytics, and explain were absent. | Requests to documented paths returned 404. | Implemented every path from `openapi.yaml`, including aggregation pipelines and explain support. |

The starter's use of `_id` internally was not itself a defect; MongoDB needs it for atomic updates, while the API removes
it from responses. The supplied sample document shapes were also retained, including legacy missing `history` and
`half_day` fields.

Also note anything you looked at and decided was **not** a defect, and why.
