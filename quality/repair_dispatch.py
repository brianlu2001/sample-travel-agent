"""Durable per-metric investigations shared by independent repair containers."""
import json
import time
import uuid

from quality import store
from quality.config import PROJECT, PRIMARY_METRICS, agent_version, fingerprint
from quality.evaluation import evaluator_version

ACTIVE = ('queued', 'waiting_baseline', 'running', 'awaiting_review', 'needs_attention')


def metric_key(source, metric):
    return fingerprint([PROJECT, source, metric])


def active(source, metric):
    rows = store.rows("SELECT * FROM repair_requests WHERE metric_key=? AND state IN ('queued','waiting_baseline','running','awaiting_review','needs_attention')",
                      (metric_key(source, metric),))
    return rows[0] if rows else None


def schedule(incident):
    """A unique active-metric index is the cross-process lock, held through review."""
    data = json.loads(incident['payload']) if isinstance(incident['payload'], str) else incident['payload']
    audit = store.setting('evaluator_audit', {})
    if (not store.setting('auto_repair', False) or audit.get('status') != 'passed'
            or audit.get('evaluator_version') != evaluator_version()):
        return None
    if (data.get('source') not in ('live', 'scenario') or data.get('benchmark_id')
            or data.get('metric') not in (*PRIMARY_METRICS, 'privacy') or not data.get('failing_run_ids')
            or data.get('version') != agent_version() or data.get('evaluator_version') != evaluator_version()):
        return None
    episode = data.get('episode_id', incident['id'])
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'metric-repair:'+episode).hex
    key = metric_key(data['source'], data['metric'])
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        # A terminal attempt is retained, not retried on every subsequent sample.
        existing = con.execute('SELECT id FROM repair_requests WHERE id=?', (identifier,)).fetchone()
        if existing:
            return existing['id']
        locked = con.execute("SELECT id FROM repair_requests WHERE metric_key=? AND state IN ('queued','waiting_baseline','running','awaiting_review','needs_attention')", (key,)).fetchone()
        if locked:
            return locked['id']
        now = time.time()
        con.execute('INSERT INTO repair_requests VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?)',
                    (identifier, key, data['metric'], incident['id'], episode, 'queued', now, now,
                     json.dumps({'source':data['source'], 'agent_version':data['version'], 'evaluator_version':data['evaluator_version']})))
        store.enqueue('repair', 'metric-repair:'+identifier, {'request_id':identifier}, con)
    return identifier


def update(identifier, state, **detail):
    store.execute('UPDATE repair_requests SET state=?,updated=?,detail=? WHERE id=?',
                  (state, time.time(), json.dumps(detail), identifier))


def compatible_baseline():
    from quality.benchmarks import configuration
    context = configuration()
    for row in store.rows("SELECT * FROM benchmarks WHERE kind IN ('baseline','checkpoint') AND status='complete' ORDER BY created DESC"):
        manifest = json.loads(row['manifest'])
        if not manifest.get('candidate') and all(manifest.get(k) == v for k,v in context.items()):
            return row['id']
    return None


def execute(identifier):
    from quality import remediation
    from quality.benchmarks import configuration
    request = store.rows('SELECT * FROM repair_requests WHERE id=?', (identifier,))[0]
    if request['state'] not in ('queued','running','waiting_baseline'):
        return
    incident = store.rows('SELECT * FROM incidents WHERE id=?', (request['incident_id'],))[0]
    data = json.loads(incident['payload'])
    audit = store.setting('evaluator_audit', {})
    if data['version'] != agent_version() or data['evaluator_version'] != evaluator_version():
        update(identifier, 'superseded', reason='Agent or evaluator changed; preserve evidence and reassess.')
        return
    if audit.get('status') != 'passed' or audit.get('evaluator_version') != evaluator_version():
        update(identifier, 'needs_attention', reason='Current evaluator requires a passed regression audit.')
        return
    remediation.live_evidence(incident)  # Validate genuine, metric-specific evidence first.
    baseline_id = compatible_baseline()
    if baseline_id is None:
        config = configuration()
        baseline_id = uuid.uuid5(uuid.NAMESPACE_URL, 'repair-baseline:'+fingerprint(config)).hex
        with store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            # Reuse a manual/scheduled full baseline already in flight. Both
            # reservation paths share this transaction lock across containers.
            for job in con.execute("SELECT payload FROM jobs WHERE kind='full_evaluation' AND state IN ('pending','running')"):
                full = json.loads(job['payload'])
                if not full.get('candidate') and full.get('configuration') == config:
                    con.execute("UPDATE repair_requests SET state='waiting_baseline',baseline_id=?,updated=? WHERE id=?",
                                (full['id'],time.time(),identifier))
                    measured = con.execute('SELECT status FROM benchmarks WHERE id=?', (full['id'],)).fetchone()
                    if measured and measured['status'] == 'complete':
                        baseline_ready(con, full['id'])
                    elif measured and measured['status'] not in ('running','complete'):
                        con.execute("UPDATE repair_requests SET state='failed',updated=?,detail=? WHERE id=?",
                                    (time.time(),json.dumps({'reason':'Shared full evaluation needs an explicit operational retry.'}),identifier))
                    return
            completed = con.execute("SELECT id FROM benchmarks WHERE id=? AND status='complete'", (baseline_id,)).fetchone()
            if completed:
                con.execute("UPDATE repair_requests SET state='queued',baseline_id=?,updated=? WHERE id=?", (baseline_id,time.time(),identifier))
                store.enqueue('repair', 'baseline-ready:'+identifier, {'request_id':identifier}, con)
                return
            dead = con.execute("SELECT id FROM jobs WHERE key=? AND state='dead'", ('repair-baseline:'+baseline_id,)).fetchone()
            if dead:
                con.execute("UPDATE repair_requests SET state='failed',baseline_id=?,updated=?,detail=? WHERE id=?",
                            (baseline_id,time.time(),json.dumps({'reason':'Shared baseline needs an explicit operational retry.'}),identifier))
                return
            con.execute("UPDATE repair_requests SET state='waiting_baseline',baseline_id=?,updated=? WHERE id=?",
                        (baseline_id, time.time(), identifier))
            store.enqueue('repair_baseline', 'repair-baseline:'+baseline_id, {'id':baseline_id, 'configuration':config}, con)
        return  # Completion of the one shared baseline wakes both metric requests.
    store.execute("UPDATE repair_requests SET state='running',baseline_id=?,updated=? WHERE id=?", (baseline_id,time.time(),identifier))
    data['validation_baseline_id'] = baseline_id
    store.execute('UPDATE incidents SET payload=? WHERE id=?', (json.dumps(data), incident['id']))
    try:
        repair_id = remediation.repair(incident['id'], baseline_id, request_id=identifier)
        repair = store.rows('SELECT * FROM repairs WHERE id=?', (repair_id,))[0]
        payload = json.loads(repair['payload'])
        status = repair['status']
        state = ('awaiting_review' if status in ('pr_open','awaiting_review') else
                 'needs_attention' if status in ('awaiting_github_access','awaiting_human_evidence') else status)
        store.execute('UPDATE repair_requests SET state=?,updated=?,repair_id=?,detail=? WHERE id=?',
                      (state,time.time(),repair_id,json.dumps({'pr_url':payload.get('pr_url'),'summary':payload.get('summary')}),identifier))
    except Exception as error:
        update(identifier, 'failed', error_type=type(error).__name__)
        raise


def baseline(payload):
    from quality import benchmarks
    identifier = payload['id']
    try:
        if payload['configuration'] != benchmarks.configuration():
            raise ValueError('Baseline configuration changed; reassessment required')
        benchmarks.create(kind='baseline', benchmark_id=identifier)
        benchmarks.run(identifier, concurrency=3)
    except Exception as error:
        baseline_failed(identifier, error)
        raise
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        baseline_ready(con, identifier)


def baseline_ready(con, identifier):
    """Commit baseline completion and dependent wakeups atomically."""
    waiting = list(con.execute("SELECT id FROM repair_requests WHERE baseline_id=? AND state='waiting_baseline'", (identifier,)))
    for request in waiting:
        con.execute("UPDATE repair_requests SET state='queued',updated=? WHERE id=?", (time.time(),request['id']))
        store.enqueue('repair', 'baseline-ready:'+request['id'], {'request_id':request['id']}, con)


def baseline_failed(identifier, error):
    store.execute("UPDATE repair_requests SET state='failed',updated=?,detail=? WHERE baseline_id=? AND state='waiting_baseline'",
                  (time.time(),json.dumps({'error_type':type(error).__name__,'stage':'baseline'}),identifier))


def retry_failed():
    """Explicit operator retry; rejected candidates are never resampled here."""
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        requests = list(con.execute("SELECT * FROM repair_requests WHERE state IN ('failed','needs_attention') ORDER BY created"))
        count = 0
        for request in requests:
            if con.execute("SELECT id FROM repair_requests WHERE metric_key=? AND id!=? AND state IN ('queued','waiting_baseline','running','awaiting_review','needs_attention')",
                           (request['metric_key'],request['id'])).fetchone():
                continue
            con.execute("UPDATE repair_requests SET state='queued',updated=? WHERE id=?", (time.time(),request['id']))
            if request['baseline_id']:
                con.execute("UPDATE jobs SET state='pending',available=?,attempts=0,lease_until=NULL,owner=NULL WHERE kind IN ('repair_baseline','full_evaluation') AND state='dead' AND json_extract(payload,'$.id')=?",
                            (time.time(),request['baseline_id']))
            store.enqueue('repair', 'repair-retry:'+request['id']+':'+uuid.uuid4().hex, {'request_id':request['id']}, con)
            count += 1
        return count


def sync_pr(pr_url):
    """Signed webhook/explicit refresh confirms actual GitHub state before release."""
    import httpx
    from quality.remediation import credential
    requests = store.rows("SELECT * FROM repair_requests WHERE state='awaiting_review' AND json_extract(detail,'$.pr_url')=?", (pr_url,))
    if not requests:
        return
    parts = pr_url.removeprefix('https://github.com/').split('/')
    if len(parts)!=4 or parts[2]!='pull' or not parts[3].isdigit():
        raise ValueError('Unsupported pull request URL')
    response = httpx.get(f'https://api.github.com/repos/{parts[0]}/{parts[1]}/pulls/{parts[3]}',
                         headers={'Authorization':'Bearer '+(credential() or '')}, timeout=20)
    response.raise_for_status()
    pr = response.json()
    if pr.get('html_url') != pr_url or pr.get('state') != 'closed':
        return
    status = 'merged' if pr.get('merged') else 'pr_closed'
    with store.connection() as con:
        for request in requests:
            con.execute('UPDATE repairs SET status=?,updated=? WHERE id=?', (status,time.time(),request['repair_id']))
            con.execute('UPDATE repair_requests SET state=?,updated=? WHERE id=?', (status,time.time(),request['id']))
        # Wake monitoring for other incidents previously blocked by this metric's PR.
        from quality.evaluation import evaluator_version
        store.enqueue('monitor', 'pr-closed:'+pr_url+':'+str(pr.get('closed_at')),
                      {'source':'live','version':agent_version(),'evaluation_version':evaluator_version()}, con)
