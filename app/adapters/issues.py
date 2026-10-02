"""Issue persistence. Queue state/retries belong exclusively to PgQueuer."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pgqueuer import AsyncpgDriver, Queries
from pgqueuer.models import TableChangedEvent

from app.adapters.postgres import vector_literal
from app.issues import ENTRYPOINT
from app.models import StoredMessage


def issue_schema(dimensions):
    return f"""
CREATE UNIQUE INDEX IF NOT EXISTS conversations_tenant_id_key ON conversations(tenant_id, id);
CREATE TABLE IF NOT EXISTS conversation_issues (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    conversation_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    issue_type text NOT NULL,
    labels text[] NOT NULL DEFAULT '{{}}',
    summary text NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK(version>0),
    processed_sequence bigint NOT NULL,
    embedding vector({dimensions}),
    embedding_version integer NOT NULL DEFAULT 0,
    embedding_model text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE(tenant_id, conversation_id, id),
    FOREIGN KEY(tenant_id, conversation_id) REFERENCES conversations(tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS conversation_one_open_issue
    ON conversation_issues(tenant_id, conversation_id) WHERE status='open';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS issue_id uuid;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS issue_sequence bigserial;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS issue_processed_at timestamptz;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS issue_completed_at timestamptz;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS issue_queue_id bigint;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery text NOT NULL DEFAULT 'unknown'
    CHECK(delivery IN ('unknown','accepted','failed'));
CREATE UNIQUE INDEX IF NOT EXISTS messages_tenant_conversation_event
    ON messages(tenant_id, conversation_id, event_id);
CREATE TABLE IF NOT EXISTS conversation_turn_sentiments (
    tenant_id text NOT NULL,
    conversation_id uuid NOT NULL,
    customer_event_id text NOT NULL,
    preceding_bot_event_id text,
    sentiment text NOT NULL CHECK(sentiment IN ('positive','neutral','negative')),
    assessed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,conversation_id,customer_event_id),
    FOREIGN KEY(tenant_id,conversation_id,customer_event_id)
        REFERENCES messages(tenant_id,conversation_id,event_id),
    FOREIGN KEY(tenant_id,conversation_id,preceding_bot_event_id)
        REFERENCES messages(tenant_id,conversation_id,event_id)
);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='messages_issue_scope_fk') THEN
        ALTER TABLE messages ADD CONSTRAINT messages_issue_scope_fk
            FOREIGN KEY(tenant_id, conversation_id, issue_id)
            REFERENCES conversation_issues(tenant_id, conversation_id, id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS messages_issue_history
    ON messages(tenant_id, conversation_id, issue_sequence);
"""


class PostgresIssueRepository:
    def __init__(self, database):
        self.database = database

    async def initialize(self):
        async with self.database.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(78231650)")
            await c.execute(issue_schema(self.database.embedding_dimensions))
            queries = Queries(AsyncpgDriver(c))
            if not await c.fetchval("SELECT to_regclass('pgqueuer')"):
                await queries.install()
            # Previously completed jobs have already been removed by PgQueuer.
            await c.execute("""UPDATE messages m SET issue_completed_at=issue_processed_at
                WHERE issue_completed_at IS NULL AND issue_processed_at IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM pgqueuer q WHERE q.id=m.issue_queue_id)""")
            # Gate older-version pending successors too. A finite far-future timestamp
            # works with PgQueuer's execute_after - now() recovery query (infinity does not).
            await c.execute("""UPDATE pgqueuer q SET execute_after='9999-01-01 UTC'
                FROM messages current WHERE q.id=current.issue_queue_id AND q.status='queued'
                AND EXISTS (SELECT 1 FROM messages previous
                    WHERE previous.tenant_id=current.tenant_id
                    AND previous.conversation_id=current.conversation_id
                    AND previous.issue_queue_id IS NOT NULL
                    AND previous.issue_completed_at IS NULL
                    AND previous.issue_sequence<current.issue_sequence)""")

    async def save_message(self, message, connection=None):
        if connection is None:
            async with self.database.pool.acquire() as c:
                return await self.save_message(message, c)
        return await connection.fetchval(
            """
            INSERT INTO messages(tenant_id,conversation_id,event_id,sender_type,body,
                                 in_scope,created_at,issue_id)
            SELECT $1,$2,$3,$4,$5,$6,$7,NULL
            WHERE EXISTS(SELECT 1 FROM conversations WHERE tenant_id=$1 AND id=$2)
            ON CONFLICT(tenant_id,event_id) DO NOTHING RETURNING id
        """,
            message.tenant_id,
            message.conversation_id,
            message.event_id,
            message.sender_type,
            message.body,
            message.in_scope,
            message.created_at,
        )

    async def history(self, conversation, limit):
        rows = await self.database.pool.fetch(
            """
            SELECT m.* FROM messages m LEFT JOIN conversation_issues i ON i.id=m.issue_id
            WHERE m.tenant_id=$1 AND m.conversation_id=$2 AND m.in_scope
                AND (m.issue_id IS NULL OR i.status='open')
            ORDER BY m.issue_sequence DESC LIMIT $3
        """,
            conversation.tenant_id,
            conversation.id,
            limit,
        )
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    async def enqueue(self, reply, customer_event):
        async with self.database.pool.acquire() as c, c.transaction():
            await c.fetchval(
                "SELECT id FROM conversations WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                reply.tenant_id,
                reply.conversation_id,
            )
            inserted = await self.save_message(reply, c)
            if not inserted:
                return
            ref = dict(
                tenant_id=reply.tenant_id,
                conversation_id=str(reply.conversation_id),
                event_id=reply.event_id,
                customer_event_id=customer_event,
            )
            ids = await Queries(AsyncpgDriver(c)).enqueue(
                ENTRYPOINT,
                json.dumps(ref).encode(),
                execute_after=timedelta(seconds=60),
            )
            await c.execute("UPDATE messages SET issue_queue_id=$1 WHERE id=$2", ids[0], inserted)
            # A successor waits for its predecessor's committed issue mutation, not a timer.
            await c.execute(
                """UPDATE pgqueuer SET execute_after='9999-01-01 UTC'::timestamptz
                WHERE id=$1 AND EXISTS (
                    SELECT 1 FROM messages m JOIN messages current ON current.id=$2
                    WHERE m.tenant_id=current.tenant_id
                      AND m.conversation_id=current.conversation_id
                      AND m.issue_queue_id IS NOT NULL AND m.issue_completed_at IS NULL
                      AND m.issue_sequence<current.issue_sequence)""",
                ids[0],
                inserted,
            )

    async def delivery(self, tenant, customer_event, outcome):
        async with self.database.pool.acquire() as c, c.transaction():
            queue_id = await c.fetchval(
                """
                UPDATE messages SET delivery=$3
                WHERE tenant_id=$1 AND event_id=$2 RETURNING issue_queue_id
            """,
                tenant,
                f"reply:{customer_event}",
                outcome,
            )
            # PgQueuer 1.4 has no public 'expedite' API. Change only the initial
            # deadline, then emit its notification model (UPDATE is statement-level).
            await c.execute(
                """UPDATE pgqueuer SET execute_after=now()
                WHERE id=$1 AND status='queued' AND attempts=0
                AND NOT EXISTS (
                    SELECT 1 FROM messages m JOIN messages current ON current.issue_queue_id=$1
                    WHERE m.tenant_id=current.tenant_id
                      AND m.conversation_id=current.conversation_id
                      AND m.issue_queue_id IS NOT NULL AND m.issue_completed_at IS NULL
                      AND m.issue_sequence<current.issue_sequence)""",
                queue_id,
            )
            queries = Queries(AsyncpgDriver(c))
            channel = queries.qbe.settings.channel
            await queries.driver.notify(
                channel,
                TableChangedEvent(
                    channel=channel,
                    sent_at=datetime.now(timezone.utc),
                    type="table_changed_event",
                    operation="update",
                    table="pgqueuer",
                ).model_dump_json(),
            )

    async def snapshot(self, ref):
        tenant, conversation = ref["tenant_id"], UUID(ref["conversation_id"])
        async with self.database.pool.acquire() as c, c.transaction(isolation="repeatable_read"):
            reply = await c.fetchrow(
                """SELECT * FROM messages
                WHERE tenant_id=$1 AND conversation_id=$2 AND event_id=$3""",
                tenant,
                conversation,
                ref["event_id"],
            )
            if reply is None or reply["sender_type"] != "BOT":
                raise ValueError("Unknown issue turn")
            customer_event = ref.get("customer_event_id")
            if not customer_event:
                if not ref["event_id"].startswith("reply:"):
                    raise ValueError("Missing issue request reference")
                customer_event = ref["event_id"][len("reply:") :]
            if not await c.fetchval(
                """SELECT EXISTS(SELECT 1 FROM messages
                WHERE tenant_id=$1 AND conversation_id=$2 AND event_id=$3
                  AND sender_type='CUSTOMER')""",
                tenant,
                conversation,
                customer_event,
            ):
                raise ValueError("Missing or cross-conversation issue request")
            if await c.fetchval(
                """SELECT EXISTS(SELECT 1 FROM messages
                WHERE tenant_id=$1 AND conversation_id=$2 AND issue_sequence<$3
                  AND issue_queue_id IS NOT NULL AND issue_completed_at IS NULL)""",
                tenant,
                conversation,
                reply["issue_sequence"],
            ):
                raise ValueError("Earlier issue turn must finish or be replayed first")
            issue = await c.fetchrow(
                """SELECT id,summary,issue_type,labels,version,
                processed_sequence FROM conversation_issues
                WHERE tenant_id=$1 AND conversation_id=$2 AND status='open'""",
                tenant,
                conversation,
            )
            processed = reply["issue_processed_at"] is not None
            rows = await c.fetch(
                """
                SELECT m.event_id,m.sender_type,m.body,m.delivery,m.issue_sequence,m.created_at
                FROM messages m LEFT JOIN conversation_issues i ON i.id=m.issue_id
                WHERE m.tenant_id=$1 AND m.conversation_id=$2 AND m.in_scope
                    AND ((m.event_id=$3 AND m.sender_type='CUSTOMER')
                      OR (m.event_id=$4 AND m.sender_type='BOT'))
                    AND (m.issue_id IS NULL OR i.status='open')
                ORDER BY CASE WHEN m.sender_type='CUSTOMER' THEN 0 ELSE 1 END
            """,
                tenant,
                conversation,
                customer_event,
                ref["event_id"],
            )
            preceding_bot = await c.fetchrow(
                """SELECT b.event_id,b.body,b.delivery,b.issue_sequence FROM messages b
                JOIN messages customer ON customer.tenant_id=b.tenant_id
                    AND customer.conversation_id=b.conversation_id
                WHERE customer.tenant_id=$1 AND customer.conversation_id=$2
                    AND customer.event_id=$3 AND b.sender_type='BOT'
                    AND b.issue_sequence<customer.issue_sequence
                ORDER BY b.issue_sequence DESC LIMIT 1""",
                tenant,
                conversation,
                customer_event,
            )
        return dict(
            customer_event_id=customer_event,
            preceding_bot=dict(preceding_bot) if preceding_bot else None,
            issue=dict(issue) if issue else None,
            processed=processed,
            finished=reply["issue_completed_at"] is not None,
            sequence=reply["issue_sequence"],
            messages=[dict(r) for r in rows] if reply["in_scope"] and len(rows) == 2 else [],
        )

    async def complete(self, ref, snapshot, details, *, sentiment=None):
        tenant, conversation = ref["tenant_id"], UUID(ref["conversation_id"])
        async with self.database.pool.acquire() as c, c.transaction():
            # Serializes the short commit with future status changes, not the model call.
            await c.fetchval(
                "SELECT id FROM conversations WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                tenant,
                conversation,
            )
            current = await c.fetchrow(
                """SELECT id,version,processed_sequence
                FROM conversation_issues WHERE tenant_id=$1 AND conversation_id=$2
                AND status='open' FOR UPDATE""",
                tenant,
                conversation,
            )
            processed = await c.fetchval(
                """SELECT issue_processed_at FROM messages
                WHERE tenant_id=$1 AND conversation_id=$2 AND event_id=$3""",
                tenant,
                conversation,
                ref["event_id"],
            )
            if processed or (current and current["processed_sequence"] >= snapshot["sequence"]):
                return
            previous = snapshot["issue"]
            if (current is None) != (previous is None) or (
                current
                and (current["id"] != previous["id"] or current["version"] != previous["version"])
            ):
                raise ValueError("Issue changed during processing; retry with fresh issue state")
            if details:
                if current:
                    issue_id = current["id"]
                    await c.execute(
                        """UPDATE conversation_issues SET summary=$2,issue_type=$3,
                        labels=$4,version=version+1,processed_sequence=$5,updated_at=now()
                        WHERE id=$1""",
                        issue_id,
                        details.summary,
                        details.issue_type,
                        details.labels,
                        snapshot["sequence"],
                    )
                else:
                    issue_id = await c.fetchval(
                        """INSERT INTO conversation_issues
                        (tenant_id,conversation_id,summary,issue_type,labels,processed_sequence)
                        VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                        tenant,
                        conversation,
                        details.summary,
                        details.issue_type,
                        details.labels,
                        snapshot["sequence"],
                    )
                await c.execute(
                    """UPDATE messages SET issue_id=$3
                    WHERE tenant_id=$1 AND conversation_id=$2 AND issue_id IS NULL AND in_scope
                    AND event_id=ANY($4::text[])""",
                    tenant,
                    conversation,
                    issue_id,
                    [m["event_id"] for m in snapshot["messages"]],
                )
            if sentiment is not None and snapshot["messages"]:
                preceding = snapshot["preceding_bot"]
                valid = await c.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM messages customer
                    WHERE customer.tenant_id=$1 AND customer.conversation_id=$2
                        AND customer.event_id=$3 AND customer.sender_type='CUSTOMER'
                        AND customer.in_scope AND ($4::text IS NULL OR EXISTS(
                            SELECT 1 FROM messages bot WHERE bot.tenant_id=customer.tenant_id
                            AND bot.conversation_id=customer.conversation_id
                            AND bot.event_id=$4 AND bot.sender_type='BOT'
                            AND bot.issue_sequence<customer.issue_sequence)))""",
                    tenant,
                    conversation,
                    snapshot["customer_event_id"],
                    preceding["event_id"] if preceding else None,
                )
                if not valid:
                    raise ValueError("Invalid sentiment message references")
                await c.execute(
                    """INSERT INTO conversation_turn_sentiments
                    (tenant_id,conversation_id,customer_event_id,preceding_bot_event_id,sentiment)
                    VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
                    tenant,
                    conversation,
                    snapshot["customer_event_id"],
                    preceding["event_id"] if preceding else None,
                    sentiment,
                )
            await c.execute(
                """UPDATE messages SET issue_processed_at=now()
                WHERE tenant_id=$1 AND conversation_id=$2 AND event_id=$3""",
                tenant,
                conversation,
                ref["event_id"],
            )

    async def sentiment_history(self, tenant_id, conversation_id, limit=100):
        """Latest assessed customer turns in chronological order; no inferred missing values."""
        rows = await self.database.pool.fetch(
            """SELECT s.*,customer.body AS customer_message, customer.issue_sequence,
                bot.body AS preceding_bot_response,bot.delivery AS preceding_bot_delivery
            FROM conversation_turn_sentiments s
            JOIN messages customer ON customer.tenant_id=s.tenant_id
                AND customer.conversation_id=s.conversation_id
                AND customer.event_id=s.customer_event_id
            LEFT JOIN messages bot ON bot.tenant_id=s.tenant_id
                AND bot.conversation_id=s.conversation_id
                AND bot.event_id=s.preceding_bot_event_id
            WHERE s.tenant_id=$1 AND s.conversation_id=$2
            ORDER BY customer.issue_sequence DESC LIMIT $3""",
            tenant_id,
            conversation_id,
            max(1, min(limit, 1000)),
        )
        return [dict(row) for row in reversed(rows)]

    async def finish(self, ref):
        """Complete the embedding phase and release the successor in one transaction."""
        tenant, conversation = ref["tenant_id"], UUID(ref["conversation_id"])
        async with self.database.pool.acquire() as c, c.transaction():
            await c.fetchval(
                "SELECT id FROM conversations WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                tenant,
                conversation,
            )
            await c.execute(
                """UPDATE messages SET issue_completed_at=now()
                WHERE tenant_id=$1 AND conversation_id=$2 AND event_id=$3
                  AND issue_processed_at IS NOT NULL AND issue_completed_at IS NULL""",
                tenant,
                conversation,
                ref["event_id"],
            )
            # Release exactly the next turn, respecting its channel-send recovery grace.
            # PgQueuer owns execution/retries; held failures continue to block successors.
            await c.execute(
                """UPDATE pgqueuer q SET execute_after=CASE WHEN m.delivery='unknown'
                    THEN greatest(now(), m.created_at + interval '60 seconds') ELSE now() END
                FROM messages m WHERE q.id=m.issue_queue_id AND q.status='queued'
                  AND m.id=(SELECT id FROM messages
                    WHERE tenant_id=$1 AND conversation_id=$2 AND issue_queue_id IS NOT NULL
                      AND issue_completed_at IS NULL ORDER BY issue_sequence LIMIT 1)""",
                tenant,
                conversation,
            )
            queries = Queries(AsyncpgDriver(c))
            channel = queries.qbe.settings.channel
            await queries.driver.notify(
                channel,
                TableChangedEvent(
                    channel=channel,
                    sent_at=datetime.now(timezone.utc),
                    type="table_changed_event",
                    operation="update",
                    table="pgqueuer",
                ).model_dump_json(),
            )

    async def embedding_target(self, ref):
        row = await self.database.pool.fetchrow(
            """SELECT i.* FROM conversation_issues i
            JOIN messages m ON m.issue_id=i.id
            WHERE m.tenant_id=$1 AND m.conversation_id=$2 AND m.event_id=$3
                AND i.status='open' AND i.embedding_version<i.version""",
            ref["tenant_id"],
            UUID(ref["conversation_id"]),
            ref["event_id"],
        )
        return dict(row) if row else None

    async def publish(self, issue, vector, model):
        return await self.database.pool.execute(
            """UPDATE conversation_issues
            SET embedding=$3::vector,embedding_version=$2,embedding_model=$4
            WHERE id=$1 AND version=$2 AND status='open'""",
            issue["id"],
            issue["version"],
            vector_literal(vector),
            model,
        )
