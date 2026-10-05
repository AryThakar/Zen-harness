-- 006: remember which column descriptions the user wrote.
-- The built-in descriptions are refreshed from the code at every start; without
-- this flag that refresh also overwrote the user's own wording.
ALTER TABLE column_registry ADD COLUMN IF NOT EXISTS edited BOOLEAN DEFAULT false;
