"""Durable source memories and conversational state. This database is not a KG."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def utcnow():
    return datetime.now(timezone.utc).isoformat()


ENTITY_TYPES = ('Person','Organization','Place','Thing')


def clean_relations(relations):
    """Validate (subject, relation, object) triples for a memory; raises on malformed input."""
    if relations is None:
        return []
    if not isinstance(relations, list) or len(relations) > 5:
        raise ValueError('relations must be a list of at most 5 items')
    out = []
    for r in relations:
        if not isinstance(r, dict):
            raise ValueError('relation must be an object')
        subject, relation, obj = r.get('subject'), r.get('relation'), r.get('object')
        if any(not isinstance(v, str) or not v.strip() or len(v) > 80 for v in (subject, relation, obj)):
            raise ValueError('relation fields must contain 1..80 characters')
        relation = relation.strip().upper().replace(' ', '_')
        if not relation.replace('_', '').isalnum() or subject.strip() == obj.strip():
            raise ValueError('invalid relation')
        out.append({'subject': subject.strip(), 'relation': relation, 'object': obj.strip(),
                    'subject_type': r.get('subject_type') if r.get('subject_type') in ENTITY_TYPES else 'Entity',
                    'object_type': r.get('object_type') if r.get('object_type') in ENTITY_TYPES else 'Entity'})
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
 turn_key TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 lease TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL DEFAULT 0,
 error TEXT, created_at TEXT NOT NULL, untracked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, turn_key TEXT NOT NULL REFERENCES turns(turn_key),
 role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')), text TEXT NOT NULL,
 created_at TEXT NOT NULL, hidden_batch INTEGER, UNIQUE(turn_key,role));
CREATE TABLE IF NOT EXISTS memories(
 id INTEGER PRIMARY KEY AUTOINCREMENT, statement TEXT NOT NULL, holder TEXT NOT NULL,
 kind TEXT NOT NULL, importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 10),
 certainty TEXT NOT NULL CHECK(certainty IN ('high','medium','low')),
 source_type TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
 valid_from TEXT NOT NULL, expires_at TEXT, invalid_at TEXT, hidden_batch INTEGER,
 created_at TEXT NOT NULL, recall_count INTEGER NOT NULL DEFAULT 0, last_recalled REAL,
 relations TEXT,
 CHECK(NOT(pinned=1 AND source_type='external')));
CREATE TABLE IF NOT EXISTS sources(
 memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES messages(id), PRIMARY KEY(memory_id,message_id));
CREATE TABLE IF NOT EXISTS forget_batches(
 id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summaries(
 id INTEGER PRIMARY KEY, text TEXT NOT NULL, upto_message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runtime(
 id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 graph_group TEXT, graph_revision INTEGER NOT NULL DEFAULT -1, graph_map TEXT NOT NULL DEFAULT '{}',
 rebuild INTEGER NOT NULL DEFAULT 1, pending_group TEXT, graph_error TEXT, graph_config TEXT,
 graph_attempts INTEGER NOT NULL DEFAULT 0, attempt_revision INTEGER NOT NULL DEFAULT -1,
 graph_retry_at REAL NOT NULL DEFAULT 0, graph_status TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS llm_calls(
 id INTEGER PRIMARY KEY AUTOINCREMENT,purpose TEXT,model TEXT,ms INTEGER,input_tokens INTEGER,
 output_tokens INTEGER,cost_usd REAL,cancelled INTEGER DEFAULT 0,cache_read_tokens INTEGER,
 cache_write_tokens INTEGER,uncached_input_tokens INTEGER,error TEXT,turn_id TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,embed_tokens INTEGER,requests INTEGER,prompt_chars INTEGER,
 turns TEXT,outcome TEXT);
CREATE INDEX IF NOT EXISTS sources_message ON sources(message_id);
CREATE TABLE IF NOT EXISTS turn_dependencies(
 turn_key TEXT NOT NULL REFERENCES turns(turn_key) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES messages(id), PRIMARY KEY(turn_key,message_id));
CREATE INDEX IF NOT EXISTS dependencies_source ON turn_dependencies(message_id);
CREATE TABLE IF NOT EXISTS response_drafts(
 turn_key TEXT PRIMARY KEY,digest TEXT NOT NULL,epoch TEXT NOT NULL,
 context_ids TEXT NOT NULL,cited TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS obsolete_graphs(group_name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS vocabulary(
 relation TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('single','multi')), promoted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relation_observations(
 relation TEXT NOT NULL, memory_id INTEGER NOT NULL, judgement TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(relation,memory_id));
"""


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and 'runtime' not in existing:
                raise ValueError('Unsupported database (older phro-secretary format): use a new memory DB path')
            conn.executescript(SCHEMA)
            # Databases created before these columns. NULL relations = never analysed (older/migrated memory).
            if 'relations' not in {r[1] for r in conn.execute('PRAGMA table_info(memories)')}:
                conn.execute('ALTER TABLE memories ADD COLUMN relations TEXT')
            # untracked=1: imported turn whose replies never recorded which memories they used.
            if 'untracked' not in {r[1] for r in conn.execute('PRAGMA table_info(turns)')}:
                conn.execute('ALTER TABLE turns ADD COLUMN untracked INTEGER NOT NULL DEFAULT 0')
            runtime = {r[1] for r in conn.execute('PRAGMA table_info(runtime)')}
            if 'graph_status' not in runtime:
                conn.execute("ALTER TABLE runtime ADD COLUMN graph_status TEXT NOT NULL DEFAULT '{}'")
            # Memory IDs whose graph episode must be replaced (relations analysed after projection).
            if 'graph_stale' not in runtime:
                conn.execute("ALTER TABLE runtime ADD COLUMN graph_stale TEXT NOT NULL DEFAULT '[]'")
            # DB path that named the current graphs; tells a moved DB from a copy (memory/service.py _adopt).
            if 'graph_owner' not in runtime:
                conn.execute('ALTER TABLE runtime ADD COLUMN graph_owner TEXT')
            # Token accounting: local embedding tokens, HTTP requests per graph session, prompt size.
            calls = {r[1] for r in conn.execute('PRAGMA table_info(llm_calls)')}
            for column in ('embed_tokens','requests','prompt_chars'):
                if column not in calls:
                    conn.execute('ALTER TABLE llm_calls ADD COLUMN '+column+' INTEGER')
            # Trace: turns a call served (JSON list) and its outcome (ids, counts, next stage; never text).
            for column in ('turns','outcome'):
                if column not in calls:
                    conn.execute('ALTER TABLE llm_calls ADD COLUMN '+column+' TEXT')
            # Labels of the relation judge from the earlier graph libraries (graphiti, then memory_engine).
            conn.execute("UPDATE llm_calls SET purpose='graph_judge' WHERE purpose IN ('graphiti','memory_engine')")
            conn.execute("UPDATE relation_observations SET judgement='engine-extracted' WHERE judgement='graphiti-extracted'")
            conn.execute('INSERT OR IGNORE INTO runtime(id,epoch) VALUES(1,?)', (uuid.uuid4().hex,))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA secure_delete=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def epoch(self):
        with self.connect() as conn:
            return conn.execute('SELECT epoch FROM runtime').fetchone()[0]

    def stage_response(self, key, text, reply, cited, context_ids, epoch):
        digest = hashlib.sha256(json.dumps([text,reply],ensure_ascii=False).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT epoch FROM runtime').fetchone()[0] != epoch:
                raise ValueError('memory snapshot is stale')
            conn.execute('DELETE FROM response_drafts WHERE created_at<?',(time.time()-600,))
            conn.execute('INSERT OR REPLACE INTO response_drafts VALUES(?,?,?,?,?,?)',
                         (key,digest,epoch,json.dumps(context_ids),json.dumps(cited),time.time()))
            conn.execute('DELETE FROM response_drafts WHERE turn_key NOT IN (SELECT turn_key FROM response_drafts ORDER BY created_at DESC LIMIT 256)')

    def record_turn(self, key, user_text, reply, cited=(), created_at=None, epoch=None, context_ids=(), require_response=False):
        if not key or not isinstance(key, str) or len(key) > 200:
            raise ValueError('turn_id must be a nonempty string up to 200 characters')
        if any(not isinstance(t, str) or len(t) > 32000 for t in (user_text, reply)):
            raise ValueError('turn text must be a string up to 32000 characters')
        digest = hashlib.sha256(json.dumps([user_text, reply], ensure_ascii=False).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if epoch is not None and conn.execute('SELECT epoch FROM runtime').fetchone()[0] != epoch:
                raise ValueError('memory snapshot is stale')
            old = conn.execute('SELECT digest FROM turns WHERE turn_key=?', (key,)).fetchone()
            if old and old[0] != digest:
                raise ValueError('turn_id already belongs to different content')
            if not old:
                if require_response:
                    draft = conn.execute('SELECT * FROM response_drafts WHERE turn_key=?',(key,)).fetchone()
                    if not draft or draft['digest']!=digest or draft['epoch']!=epoch:
                        raise ValueError('commit requires the matching server response')
                    context_ids = json.loads(draft['context_ids'])
                    cited = json.loads(draft['cited'])
                dependencies = set(context_ids or [])
                if any(type(mid) is not int for mid in dependencies):
                    raise ValueError('context source IDs must be integers')
                for mid in cited or []:
                    dependencies.update(r[0] for r in conn.execute('SELECT message_id FROM sources WHERE memory_id=?',(int(mid),)))
                for mid in dependencies:
                    if not conn.execute('SELECT 1 FROM messages WHERE id=? AND hidden_batch IS NULL',(mid,)).fetchone():
                        raise ValueError('context source missing or archived')
                stamp = created_at or utcnow()
                conn.execute('INSERT INTO turns(turn_key,digest,created_at) VALUES(?,?,?)', (key,digest,stamp))
                conn.executemany('INSERT INTO turn_dependencies VALUES(?,?)',[(key,mid) for mid in dependencies])
                conn.executemany('INSERT INTO messages(turn_key,role,text,created_at) VALUES(?,?,?,?)',
                                 [(key,'user',user_text,stamp),(key,'assistant',reply,stamp)])
                for mid in set(cited or []):
                    conn.execute('UPDATE memories SET recall_count=recall_count+1,last_recalled=?'
                                 ' WHERE id=? AND hidden_batch IS NULL', (time.time(), int(mid)))
                conn.execute('DELETE FROM response_drafts WHERE turn_key=?',(key,))
            rows = conn.execute('SELECT id,role FROM messages WHERE turn_key=?', (key,)).fetchall()
            return {r['role'] + '_message': r['id'] for r in rows}

    def _confirm(self, conn, claims, epoch):
        if conn.execute('SELECT epoch FROM runtime').fetchone()[0] != epoch:
            raise ValueError('memory snapshot is stale')
        ids = []
        for claim in claims:
            statement = claim.get('statement')
            source_ids = claim.get('source_ids')
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 3000:
                raise ValueError('memory statement must contain 1..3000 characters')
            if (not isinstance(source_ids, list) or not source_ids
                    or any(type(i) is not int for i in source_ids)):
                raise ValueError('confirmed memory requires source message IDs')
            source_ids = sorted(set(source_ids))
            marks = ','.join('?' for _ in source_ids)
            sources = conn.execute('SELECT * FROM messages WHERE hidden_batch IS NULL AND id IN ('+marks+')', source_ids).fetchall()
            if len(sources) != len(source_ids):
                raise ValueError('unknown or hidden source message')
            kind = claim.get('kind', 'knowledge')
            if kind not in ('profile','preference','decision','plan','event','knowledge','summary'):
                raise ValueError('unknown memory kind')
            source_type = claim.get('source_type', 'user')
            if source_type not in ('user','assistant_confirmed','external'):
                raise ValueError('unknown source type')
            if source_type == 'user' and any(s['role'] != 'user' for s in sources):
                raise ValueError('user memory requires user evidence')
            if type(claim.get('importance',5)) is not int:
                raise ValueError('importance must be an integer')
            holder = claim.get('holder', '사용자')
            if not isinstance(holder, str) or not holder.strip() or len(holder) > 200:
                raise ValueError('invalid memory holder')
            relations = clean_relations(claim['relations']) if claim.get('relations') is not None else None
            valid_from = claim.get('valid_from') or sources[0]['created_at']
            expires_at = claim.get('expires_at')
            for stamp in (valid_from, expires_at):
                if stamp is not None:
                    datetime.fromisoformat(stamp.replace('Z','+00:00'))
            # Reprocessing the same evidence must not create another memory or recall event.
            found = None
            for row in conn.execute('SELECT id FROM memories WHERE statement=? AND holder=? AND hidden_batch IS NULL', (statement.strip(),holder)):
                known = {r[0] for r in conn.execute('SELECT message_id FROM sources WHERE memory_id=?', (row[0],))}
                if known == set(source_ids):
                    found = row[0]
                    break
            if found is not None:
                ids.append(found)
                continue
            mid = conn.execute('INSERT INTO memories(statement,holder,kind,importance,certainty,source_type,pinned,'
                               'valid_from,expires_at,created_at,relations) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (statement.strip(),holder,kind,claim.get('importance',5),claim.get('certainty','high'),
                                source_type,int(bool(claim.get('pinned',False))),valid_from,expires_at,utcnow(),
                                None if relations is None else json.dumps(relations,ensure_ascii=False))).lastrowid
            conn.executemany('INSERT INTO sources VALUES(?,?)', [(mid,s) for s in source_ids])
            conn.execute('UPDATE runtime SET revision=revision+1 WHERE id=1')
            ids.append(mid)
        return ids

    def confirm(self, claims, epoch):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._confirm(conn, claims, epoch)

    def visible_memories(self, conn=None):
        if conn is None:
            with self.connect() as conn:
                return self.visible_memories(conn)
        rows = [dict(r) for r in conn.execute('SELECT * FROM memories WHERE hidden_batch IS NULL ORDER BY id')]
        sources = {}
        for source in conn.execute('SELECT s.memory_id,s.message_id FROM sources s JOIN memories m ON m.id=s.memory_id WHERE m.hidden_batch IS NULL ORDER BY s.message_id'):
            sources.setdefault(source[0],[]).append(source[1])
        for row in rows:
            row['source_ids'] = sources.get(row['id'],[])
            row['relations'] = None if row['relations'] is None else json.loads(row['relations'])
        return rows

    def context(self, limit=10):
        return self.context_snapshot(limit=limit)[0]

    def context_snapshot(self, memory_ids=(), limit=10):
        with self.connect() as conn:
            conn.execute('BEGIN')
            summary = conn.execute('SELECT text,upto_message_id FROM summaries ORDER BY id DESC LIMIT 1').fetchone()
            rows = list(conn.execute("SELECT id,role,text FROM messages WHERE hidden_batch IS NULL AND text<>'' ORDER BY id DESC LIMIT ?", (limit,)))[::-1]
            dependencies = {r['id'] for r in rows}
            if summary:
                dependencies.update(r[0] for r in conn.execute('SELECT id FROM messages WHERE hidden_batch IS NULL AND id<=?',(summary[1],)))
            for mid in memory_ids:
                dependencies.update(r[0] for r in conn.execute('SELECT message_id FROM sources WHERE memory_id=?',(mid,)))
            text = '\n'.join(([summary[0]] if summary else []) + [r['role']+': '+r['text'] for r in rows]) or '(없음)'
            return text, sorted(dependencies)

    def claim_turn(self):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM turns WHERE (status='pending' AND retry_at<=?)"
                               " OR (status='running' AND lease_until<?) ORDER BY created_at,turn_key LIMIT 1",
                               (time.time(),time.time())).fetchone()
            if not row:
                return None
            turn = dict(row)
            turn['lease'] = uuid.uuid4().hex
            conn.execute("UPDATE turns SET status='running',lease=?,lease_until=?,attempts=attempts+1 WHERE turn_key=?",
                         (turn['lease'],time.time()+600,turn['turn_key']))
            turn['messages'] = [dict(r) for r in conn.execute('SELECT * FROM messages WHERE turn_key=? AND hidden_batch IS NULL ORDER BY id', (turn['turn_key'],))]
            turn['epoch'] = conn.execute('SELECT epoch FROM runtime').fetchone()[0]
            return turn

    def finish_turn(self, key, lease, claims, epoch):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT 1 FROM turns WHERE turn_key=? AND lease=? AND status='running'", (key,lease)).fetchone()
            if not row:
                raise ValueError('turn lease is stale')
            allowed = {r[0] for r in conn.execute('SELECT id FROM messages WHERE turn_key=?', (key,))}
            if any(not set(c.get('source_ids',[])) <= allowed for c in claims):
                raise ValueError('source outside claimed turn')
            ids = self._confirm(conn, claims, epoch)
            conn.execute("UPDATE turns SET status='done',lease=NULL,error=NULL WHERE turn_key=? AND lease=?", (key,lease))
            return ids

    def fail_turn(self, key, lease, error):
        with self.connect() as conn:
            conn.execute("UPDATE turns SET status=CASE WHEN attempts>=5 THEN 'dead' ELSE 'pending' END,"
                         "retry_at=?,error=?,lease=NULL WHERE turn_key=? AND lease=?",
                         (time.time()+30,error[:80],key,lease))

    def _invalidate(self, conn):
        # New epoch cancels in-flight turns; the worker then reconciles the graph incrementally
        # (removes episodes of memories no longer visible, adds restored ones). No full rebuild.
        conn.execute('UPDATE runtime SET epoch=?,revision=revision+1 WHERE id=1', (uuid.uuid4().hex,))
        conn.execute('DELETE FROM summaries')
        conn.execute('DELETE FROM response_drafts')

    def forget(self, memory_ids=(), message_ids=(), reason='', dry_run=False):
        """Archive memories/messages and what depends on them. dry_run returns the same scope unchanged.

        Two kinds of closure:
        - whole turns: turns holding forgotten evidence (sources of forgotten memories, requested messages).
          Every message of such a turn is hidden, and every memory any of them sourced is forgotten too.
        - replies only: later turns whose reply may repeat hidden text — they had a hidden message in their
          context (turn_dependencies), or they are imported (untracked) turns from the earliest forgotten
          message on, whose dependencies were never recorded. Only their assistant messages are hidden; the
          user's own words and the memories confirmed from them stay, because they do not derive from the
          forgotten fact. (Hiding whole dependent turns took unrelated memories along: forgetting one fact
          hid the next 11 turns and a later address correction, docs/troubleshooting.md.)
        """
        ids, whole = set(map(int,memory_ids)), set(map(int,message_ids))
        if not ids and not whole:
            raise ValueError('forget requires explicit IDs')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for mid in ids:
                if not conn.execute('SELECT 1 FROM memories WHERE id=? AND hidden_batch IS NULL', (mid,)).fetchone():
                    raise ValueError('memory missing or already archived')
            visible = lambda mid: (row := conn.execute('SELECT hidden_batch FROM messages WHERE id=?', (mid,)).fetchone()) and row[0] is None
            for mid in whole:
                if not visible(mid):
                    raise ValueError('source missing or overlapping archive')
            replies = set()
            while True:
                before = (len(ids),len(whole),len(replies))
                for mid in list(ids):
                    for (source,) in conn.execute('SELECT message_id FROM sources WHERE memory_id=?', (mid,)):
                        if source not in whole and not visible(source):
                            raise ValueError('source missing or overlapping archive')
                        whole.add(source)
                for mid in list(whole):
                    # Turn mates already archived by another batch stay in that batch.
                    whole.update(r[0] for r in conn.execute('SELECT m.id FROM messages m JOIN messages x ON x.turn_key=m.turn_key'
                                                             ' WHERE x.id=? AND m.hidden_batch IS NULL', (mid,)))
                    ids.update(r[0] for r in conn.execute('SELECT memory_id FROM sources WHERE message_id=?', (mid,)))
                hidden = whole | replies
                for mid in list(hidden):
                    replies.update(r[0] for r in conn.execute(
                        "SELECT m.id FROM turn_dependencies d JOIN messages m ON m.turn_key=d.turn_key"
                        " WHERE d.message_id=? AND m.role<>'user' AND m.hidden_batch IS NULL", (mid,)))
                if hidden:
                    marks = ','.join('?' for _ in hidden)
                    earliest = conn.execute('SELECT MIN(created_at) FROM messages WHERE id IN ('+marks+')', list(hidden)).fetchone()[0]
                    replies.update(r[0] for r in conn.execute("SELECT m.id FROM messages m JOIN turns t ON t.turn_key=m.turn_key"
                        " WHERE t.untracked=1 AND m.role<>'user' AND m.hidden_batch IS NULL AND m.created_at>=?", (earliest,)))
                replies -= whole
                if before == (len(ids),len(whole),len(replies)):
                    break
            mids = whole | replies
            if any(conn.execute('SELECT hidden_batch FROM memories WHERE id=?', (mid,)).fetchone()[0] is not None for mid in ids):
                raise ValueError('overlapping archive')
            def turns_of(messages, untracked=False):
                if not messages:
                    return 0
                marks = ','.join('?' for _ in messages)
                return conn.execute('SELECT COUNT(DISTINCT t.turn_key) FROM messages m JOIN turns t ON t.turn_key=m.turn_key'
                                    ' WHERE m.id IN ('+marks+')'+(' AND t.untracked=1' if untracked else ''), list(messages)).fetchone()[0]
            # turns: whole turns hidden; reply_turns: later turns whose reply alone is hidden.
            scope = {'memory_ids':sorted(ids),'message_ids':sorted(mids),'turns':turns_of(whole),
                     'reply_turns':turns_of(replies),'untracked_turns':turns_of(mids,untracked=True)}
            if dry_run:
                return scope
            batch = conn.execute("INSERT INTO forget_batches(reason,status,created_at) VALUES(?,'archived',?)", (reason,utcnow())).lastrowid
            conn.executemany('UPDATE memories SET hidden_batch=? WHERE id=?', [(batch,i) for i in ids])
            conn.executemany('UPDATE messages SET hidden_batch=? WHERE id=?', [(batch,i) for i in mids])
            self._invalidate(conn)
            return {'batch':batch,**scope}

    def restore(self, batch):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM forget_batches WHERE id=? AND status='archived'", (batch,)).fetchone():
                raise ValueError('batch is not restorable')
            conn.execute('UPDATE memories SET hidden_batch=NULL WHERE hidden_batch=?', (batch,))
            # A reply hidden here whose user message a later batch archived would come back alone; it moves to
            # that batch instead and returns (or is purged) with its turn.
            conn.execute("UPDATE messages SET hidden_batch=(SELECT u.hidden_batch FROM messages u WHERE u.turn_key=messages.turn_key"
                         " AND u.role='user' AND u.hidden_batch IS NOT NULL AND u.hidden_batch<>?) WHERE hidden_batch=? AND role<>'user'"
                         " AND EXISTS(SELECT 1 FROM messages u WHERE u.turn_key=messages.turn_key AND u.role='user'"
                         " AND u.hidden_batch IS NOT NULL AND u.hidden_batch<>?)", (batch,batch,batch))
            conn.execute('UPDATE messages SET hidden_batch=NULL WHERE hidden_batch=?', (batch,))
            conn.execute("UPDATE forget_batches SET status='restored' WHERE id=?", (batch,))
            self._invalidate(conn)
            return {'batch':batch,'restored':True}

    def purge(self, batch):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM forget_batches WHERE id=?', (batch,)).fetchone()
            if row and row[0] == 'purged':
                return {'batch':batch,'purged':True}
            if not row or row[0] != 'archived':
                raise ValueError('archive before purge')
            conn.execute('DELETE FROM memories WHERE hidden_batch=?', (batch,))
            conn.execute('DELETE FROM relation_observations WHERE memory_id NOT IN (SELECT id FROM memories)')
            conn.execute("UPDATE turns SET digest='' WHERE turn_key IN (SELECT turn_key FROM messages WHERE hidden_batch=?)", (batch,))
            conn.execute("UPDATE messages SET text='' WHERE hidden_batch=?", (batch,))
            conn.execute("UPDATE forget_batches SET status='purged',reason='' WHERE id=?", (batch,))
            self._invalidate(conn)
            return {'batch':batch,'purged':True,'graph_cleanup_pending':True}

    def vocabulary(self):
        """Relations the user promoted from the expansion candidates (memory/graph.py adds them to its rules)."""
        with self.connect() as conn:
            rows = conn.execute('SELECT relation,kind FROM vocabulary').fetchall()
        return {'single':{r[0] for r in rows if r[1]=='single'},'multi':{r[0] for r in rows if r[1]=='multi'}}

    def promote(self, relation, kind):
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO vocabulary VALUES(?,?,?)', (relation,kind,utcnow()))

    def demote(self, relation):
        with self.connect() as conn:
            return conn.execute('DELETE FROM vocabulary WHERE relation=?', (relation,)).rowcount

    def observe(self, observations):
        """Record how relations outside the vocabulary were settled: (relation, memory_id, judgement)."""
        if observations:
            with self.connect() as conn:
                conn.executemany('INSERT OR REPLACE INTO relation_observations VALUES(?,?,?,?)',
                                 [(r,m,j,utcnow()) for r,m,j in observations])

    def audit_static(self, purpose, turn_key, outcome):
        """A step decided by rule without any model call, kept in the record next to the calls it replaced."""
        with self.connect() as conn:
            conn.execute("INSERT INTO llm_calls(purpose,model,ms,turn_id,turns,outcome) VALUES(?,'rule',0,?,?,?)",
                         (purpose,turn_key,json.dumps([turn_key]),json.dumps(outcome,ensure_ascii=False)))

    def annotate_call(self, row_id, outcome):
        with self.connect() as conn:
            conn.execute('UPDATE llm_calls SET outcome=? WHERE id=?', (json.dumps(outcome,ensure_ascii=False),row_id))

    def turns_of(self, memory_ids=(), message_ids=()):
        """Conversation turns behind memories (through their sources) and messages."""
        memory_ids, message_ids = list(memory_ids), list(message_ids)
        with self.connect() as conn:
            keys = set()
            for i in range(0,len(memory_ids),500):
                part = memory_ids[i:i+500]
                keys |= {r[0] for r in conn.execute('SELECT DISTINCT m.turn_key FROM sources s JOIN messages m ON m.id=s.message_id'
                                                    ' WHERE s.memory_id IN (%s)' % ','.join('?'*len(part)), part)}
            for i in range(0,len(message_ids),500):
                part = message_ids[i:i+500]
                keys |= {r[0] for r in conn.execute('SELECT DISTINCT turn_key FROM messages WHERE id IN (%s)' % ','.join('?'*len(part)), part)}
        return sorted(keys)

    def graph_state(self):
        with self.connect() as conn:
            state = dict(conn.execute('SELECT * FROM runtime').fetchone())
        state['episode_map'] = json.loads(state.pop('graph_map'))
        state['memory_status'] = {int(k):v for k,v in json.loads(state.pop('graph_status')).items()}
        state['stale'] = set(json.loads(state.pop('graph_stale')))
        state['ready'] = bool(state['graph_group']) and not state['rebuild'] and not state['pending_group']
        return state

    def projection_input(self):
        with self.connect() as conn:
            conn.execute('BEGIN')
            return conn.execute('SELECT revision FROM runtime').fetchone()[0], self.visible_memories(conn)

    def pending_projection(self, group):
        with self.connect() as conn:
            conn.execute('UPDATE runtime SET pending_group=?,graph_owner=?,graph_attempts=CASE WHEN attempt_revision=revision'
                         ' THEN graph_attempts+1 ELSE 1 END,attempt_revision=revision WHERE id=1', (group,self.path))

    def publish_projection(self, revision, group, episode_map, config=None, status=None):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT graph_group FROM runtime WHERE revision=?', (revision,)).fetchone()
            changed = bool(conn.execute('UPDATE runtime SET graph_group=?,graph_revision=?,graph_map=?,rebuild=0,'
                                     'pending_group=NULL,graph_error=NULL,graph_attempts=0,graph_retry_at=0,graph_config=?,graph_status=?,'
                                     "graph_stale='[]' WHERE id=1 AND revision=?",
                                     (group,revision,json.dumps(episode_map),config,json.dumps(status or {}),revision)).rowcount)
            if changed and previous and previous[0] and previous[0] != group:
                conn.execute('INSERT OR IGNORE INTO obsolete_graphs VALUES(?)', (previous[0],))
            return changed

    def disown_graphs(self, names):
        """Forget graphs named under another DB path; the next projection builds this path's own."""
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE runtime SET graph_group=NULL,pending_group=NULL,graph_map='{}',graph_status='{}',"
                         "graph_stale='[]',rebuild=1,graph_owner=? WHERE id=1", (self.path,))
            conn.executemany('DELETE FROM obsolete_graphs WHERE group_name=?', [(n,) for n in names])

    def projection_failed(self, error):
        with self.connect() as conn:
            attempts = conn.execute('SELECT graph_attempts FROM runtime').fetchone()[0]
            conn.execute('UPDATE runtime SET rebuild=1,graph_error=?,graph_retry_at=? WHERE id=1',
                         (error[:120],time.time()+min(300,5*2**min(attempts,6))))

    def set_relations(self, relations):
        """Record relations for still-unanalysed visible memories; their graph episodes are replaced."""
        if not relations:
            return 0
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = [int(mid) for mid,r in relations.items() if conn.execute(
                'UPDATE memories SET relations=? WHERE id=? AND relations IS NULL AND hidden_batch IS NULL',
                (json.dumps(clean_relations(r),ensure_ascii=False),int(mid))).rowcount]
            if changed:
                # Conversation context is unchanged, so no epoch change: only these episodes are re-projected.
                stale = set(json.loads(conn.execute('SELECT graph_stale FROM runtime').fetchone()[0])) | set(changed)
                conn.execute('UPDATE runtime SET revision=revision+1,graph_stale=? WHERE id=1', (json.dumps(sorted(stale)),))
            return len(changed)

    def retry(self):
        with self.connect() as conn:
            conn.execute("UPDATE turns SET status='pending',attempts=0,retry_at=0 WHERE status='dead'")
            conn.execute('UPDATE runtime SET graph_attempts=0,graph_retry_at=0 WHERE id=1')
        return {'retry_queued':True}

    def reset(self):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Promoted vocabulary is the user's decision about relations, not memory content: it survives a reset.
            for table in ('turn_dependencies','sources','memories','messages','turns','forget_batches','summaries','llm_calls',
                          'relation_observations'):
                conn.execute('DELETE FROM '+table)
            self._invalidate(conn)
            conn.execute("UPDATE runtime SET rebuild=1,graph_stale='[]' WHERE id=1")
        return {'reset':True}
