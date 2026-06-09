-- Migration 001 Rollback: Drop LangGraph Checkpoint Tables
-- WARNING: This will delete all checkpoint data and agent execution history

-- Drop indexes first
DROP INDEX IF EXISTS checkpoint_writes_thread_id_idx;
DROP INDEX IF EXISTS checkpoint_blobs_thread_id_idx;
DROP INDEX IF EXISTS checkpoints_thread_id_idx;

-- Drop tables in reverse dependency order
DROP TABLE IF EXISTS checkpoint_writes;
DROP TABLE IF EXISTS checkpoint_blobs;
DROP TABLE IF EXISTS checkpoints;

-- Remove migration version tracking
DELETE FROM checkpoint_migrations WHERE v = 9;

-- Drop migration table if no other migrations exist
-- This is safe because it's the first migration
DROP TABLE IF EXISTS checkpoint_migrations;
