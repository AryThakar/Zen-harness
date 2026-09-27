-- 004: ids come from nextval() in the INSERT, not from column defaults.

-- DuckDB (1.5) can't replay an ALTER TABLE from its write-ahead log when the table
-- has a DEFAULT nextval(...) column: after a crash the database file won't open.
-- Custom columns are added to sales with ALTER TABLE, so every upload with a new
-- custom column was exposed until the next checkpoint. Without the defaults the
-- log replays cleanly; the inserts now call nextval() themselves.
ALTER TABLE sales ALTER COLUMN sale_id DROP DEFAULT;
ALTER TABLE format_profiles ALTER COLUMN profile_id DROP DEFAULT;
ALTER TABLE ml_runs ALTER COLUMN run_id DROP DEFAULT;
