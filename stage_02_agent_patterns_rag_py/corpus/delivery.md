# Durable report delivery

An outbox record stores the report and its stable delivery key in the same database transaction. A worker can restart and read pending records without rebuilding the report.

A timeout after delivery leaves the worker unsure whether the receiver accepted the report. Retry with the same idempotency key. The receiver must deduplicate that key and retain the recorded result for the retry window.
