# DECISIONS.md  (about 150-200 words in total - plain language, your own words)
1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject? 
i.employees: Unique index on emp_code (enforces uniqueness and speeds lookups); compound index on {department: 1, joined_on: 1, emp_code: 1} for fast department trend/summary queries.
ii.attendance_logs: Unique compound index on {emp_code: 1, date: 1} (prevents duplicate punch-ins and powers regularize/monthly queries); compound index on {date: -1, emp_code: 1} for fast paginated listing; compound index on {emp_code: 1, punch_in: 1, punch_out: 1} to quickly find open punch-ins.
iii.Rejected: Index on history.at because history is an internal audit array never filtered in query predicates.
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
i.Both concurrent requests calculate the IST attendance date and attempt to insert into attendance_logs.
ii.The database unique index on {emp_code: 1, date: 1} atomically permits exactly one insertion.
iii.The winning thread completes insertion and returns HTTP 201.
iv.The losing thread receives a DuplicateKeyError from PyMongo, which is caught and returned as HTTP 409 (already punched in for this date).
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
i.Uses standard competition ranking ($rank in $setWindowFields). Tied employees receive equal rank (e.g. 1, 2, 2, 4).
ii.limit filters after ranking (rank <= limit). If employees tie at the cutoff rank (e.g., rank 10), all tied employees are returned (exceeding limit item count if necessary) to strictly uphold ranking fairness. 
iii.Rows are deterministically sorted by total_late_minutes descending, then emp_code ascending.
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
i.The aggregation starts on employees (headcount source of truth) filtered by joined_on <= month_end.
ii.A $lookup joins attendance_logs for the target month. Employees with zero logs yield an empty joined array ([]) but remain in the pipeline, guaranteeing they are included in headcount totals.
5. **One thing you would change** if this had to serve 100x the data.
I would move from on-demand MongoDB aggregation pipelines over raw log documents to an event-driven, pre-aggregated analytics collection maintained via Change Streams or background workers. Analytics endpoints would perform O(1) document lookups rather than scanning historical log indexes.
