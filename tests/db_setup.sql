-- Test database setup: minimal tables needed by harness-runtime integration tests.
-- The `chat_messages` table is owned by the SDK (Drizzle schema) but written by
-- the harness-runtime's message_writer module. We create it here so integration
-- tests can run without the SDK's full migration pipeline.

CREATE TABLE IF NOT EXISTS chat_messages (
    id text PRIMARY KEY NOT NULL,
    session_id text NOT NULL,
    role text NOT NULL,
    content jsonb NOT NULL,
    message jsonb,
    sequence integer NOT NULL,
    source text DEFAULT 'stream' NOT NULL,
    created_at timestamp DEFAULT now() NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_session_msg_id
    ON chat_messages USING btree (session_id, (message->>'id'));
