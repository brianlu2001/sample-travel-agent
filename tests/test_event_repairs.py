"""Isolated queue/lifecycle tests. No model calls or exported test telemetry."""
import concurrent.futures
import hashlib
import hmac
import json
import threading
import time
from types import SimpleNamespace

import pytest

from quality import store, repair_dispatch as dispatch
from quality.config import agent_version
from quality.evaluation import evaluator_version


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'STATE',tmp_path)
    monkeypatch.setattr(store,'DB',tmp_path/'events.sqlite3')
    store.init()
    store.set_setting('auto_repair',True)
    store.set_setting('evaluator_audit',{'status':'passed','evaluator_version':evaluator_version()})


def incident(identifier, metric='correctness'):
    payload = {'source':'live','metric':metric,'version':agent_version(),'evaluator_version':evaluator_version(),
               'episode_id':identifier,'failing_run_ids':['isolated-evidence'],'window_run_ids':['isolated-evidence']}
    store.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,NULL)',
                  (identifier,identifier,'open',time.time(),time.time(),json.dumps(payload)))
    return store.rows('SELECT * FROM incidents WHERE id=?',(identifier,))[0]


def test_duplicate_deliveries_and_containers_reserve_one_request_per_metric():
    cases = [incident('a'),incident('b','groundedness'),incident('c')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(dispatch.schedule, cases*8))
    assert len(store.rows('SELECT * FROM repair_requests'))==2
    assert len(store.rows("SELECT * FROM jobs WHERE kind='repair'"))==2


def test_live_evaluations_overtake_offline_backlog_without_losing_jobs():
    from quality.worker import claim_job
    for i in range(60):
        store.enqueue('evaluate','offline:'+str(i),{'run_id':str(i)})
    store.save_run({'id':'live-test','created':time.time(),'source':'live','version':agent_version(),'privacy_ok':True})
    first=claim_job(('evaluate_live','evaluate'),600)
    assert first['payload']['run_id']=='live-test'
    store.finish(first)
    second=claim_job(('evaluate_live','evaluate'),600)
    assert second['key']=='offline:0'
    assert len(store.rows("SELECT id FROM jobs WHERE state='pending'"))==59


def test_pending_pr_blocks_same_metric_even_across_incidents_then_terminal_releases():
    first = dispatch.schedule(incident('first'))
    dispatch.update(first,'awaiting_review',pr_url='https://github.com/example/repo/pull/1')
    second = incident('second')
    assert dispatch.schedule(second)==first
    assert dispatch.schedule(incident('ground','groundedness'))!=first
    dispatch.update(first,'rejected')
    assert dispatch.schedule(second)!=first
    assert dispatch.schedule(store.rows('SELECT * FROM incidents WHERE id=?',('first',))[0])==first
    assert len(store.rows('SELECT * FROM repair_requests'))==3


def test_different_metrics_execute_concurrently_and_keep_review_locks(monkeypatch):
    from quality import remediation
    requests=[dispatch.schedule(incident('a')),dispatch.schedule(incident('b','groundedness'))]
    monkeypatch.setattr(dispatch,'compatible_baseline',lambda:'sealed')
    monkeypatch.setattr(remediation,'live_evidence',lambda _: [{'run_id':'isolated'}])
    barrier=threading.Barrier(2)
    def repair(incident_id, baseline_id, request_id):
        assert baseline_id=='sealed'
        barrier.wait(timeout=3)
        store.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)',
                      (request_id,incident_id,'pr_open',time.time(),time.time(),json.dumps({'pr_url':'https://github.com/example/repo/pull/'+incident_id})))
        return request_id
    monkeypatch.setattr(remediation,'repair',repair)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch.execute,requests))
    assert {r['state'] for r in store.rows('SELECT state FROM repair_requests')}=={'awaiting_review'}


def test_concurrent_requests_share_one_baseline_and_resume_only_after_completion(monkeypatch):
    from quality import remediation,benchmarks
    requests=[dispatch.schedule(incident('a')),dispatch.schedule(incident('b','groundedness'))]
    monkeypatch.setattr(dispatch,'compatible_baseline',lambda:None)
    monkeypatch.setattr(remediation,'live_evidence',lambda _: [{'run_id':'isolated'}])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch.execute,requests))
    jobs=store.rows("SELECT * FROM jobs WHERE kind='repair_baseline'")
    assert len(jobs)==1
    assert {r['state'] for r in store.rows('SELECT state FROM repair_requests')}=={'waiting_baseline'}
    order=[]
    monkeypatch.setattr(benchmarks,'create',lambda **kw:order.append('create'))
    monkeypatch.setattr(benchmarks,'run',lambda *a,**kw:order.append('measure'))
    dispatch.baseline(json.loads(jobs[0]['payload']))
    assert order==['create','measure']
    assert {r['state'] for r in store.rows('SELECT state FROM repair_requests')}=={'queued'}
    assert len(store.rows("SELECT * FROM jobs WHERE key LIKE 'baseline-ready:%'"))==2


def test_annotation_ack_emits_once_and_failure_does_not_emit(monkeypatch):
    from quality import worker
    monkeypatch.setattr('quality.monitoring.current_window',lambda *a,**kw:[])
    data={'source':'live','version':agent_version(),'evaluation_version':evaluator_version(),'run_id':'isolated'}
    job={'key':'annotations:isolated','kind':'annotations','payload':{'annotations':[{'span_id':'isolated'}],'evaluation_event':data}}
    def unavailable(**kw):
        raise ConnectionError('isolated test')
    monkeypatch.setattr(worker,'PHOENIX_CLIENT',SimpleNamespace(spans=SimpleNamespace(log_span_annotations=unavailable)))
    with pytest.raises(ConnectionError):
        worker.dispatch(job)
    assert not store.rows("SELECT * FROM jobs WHERE kind='monitor'")
    monkeypatch.setattr(worker.PHOENIX_CLIENT.spans,'log_span_annotations',lambda **kw:None)
    worker.dispatch(job)
    worker.dispatch(job)
    assert len(store.rows("SELECT * FROM jobs WHERE kind='monitor'"))==1
    job['key']='annotations:benchmark'
    data['source']='benchmark'
    worker.dispatch(job)
    assert len(store.rows("SELECT * FROM jobs WHERE kind='monitor'"))==1


def test_repair_reuses_scheduled_baseline_and_failure_releases_locks(monkeypatch):
    from quality import remediation,benchmarks
    identifier=dispatch.schedule(incident('a'))
    monkeypatch.setattr(dispatch,'compatible_baseline',lambda:None)
    monkeypatch.setattr(remediation,'live_evidence',lambda _: [{'run_id':'isolated'}])
    store.enqueue('full_evaluation','full:existing',{'id':'existing','candidate':None,'configuration':benchmarks.configuration()})
    dispatch.execute(identifier)
    request=store.rows('SELECT * FROM repair_requests')[0]
    assert request['state']=='waiting_baseline' and request['baseline_id']=='existing'
    assert not store.rows("SELECT * FROM jobs WHERE kind='repair_baseline'")
    dispatch.baseline_failed('existing',ValueError('isolated'))
    assert dispatch.active('live','correctness') is None


def test_configuration_change_does_not_strand_waiting_metric(monkeypatch):
    from quality import remediation
    identifier=dispatch.schedule(incident('a'))
    monkeypatch.setattr(dispatch,'compatible_baseline',lambda:None)
    monkeypatch.setattr(remediation,'live_evidence',lambda _: [{'run_id':'isolated'}])
    dispatch.execute(identifier)
    payload=json.loads(store.rows("SELECT payload FROM jobs WHERE kind='repair_baseline'")[0]['payload'])
    payload['configuration']={'changed':True}
    with pytest.raises(ValueError):
        dispatch.baseline(payload)
    assert dispatch.active('live','correctness') is None


def test_full_baseline_completing_during_reservation_wakes_request(monkeypatch):
    from quality import remediation,benchmarks
    identifier=dispatch.schedule(incident('a'))
    monkeypatch.setattr(dispatch,'compatible_baseline',lambda:None)
    monkeypatch.setattr(remediation,'live_evidence',lambda _: [{'run_id':'isolated'}])
    store.enqueue('full_evaluation','full:existing',{'id':'existing','candidate':None,'configuration':benchmarks.configuration()})
    store.execute("INSERT INTO benchmarks VALUES('existing','baseline',NULL,'complete',1,2,'{}','{}',NULL,NULL)")
    dispatch.execute(identifier)
    assert store.rows('SELECT state FROM repair_requests')[0]['state']=='queued'
    assert len(store.rows("SELECT * FROM jobs WHERE key LIKE 'baseline-ready:%'"))==1


def test_operational_retry_cannot_compete_with_newer_active_request():
    first=dispatch.schedule(incident('a'))
    dispatch.update(first,'failed')
    second=dispatch.schedule(incident('b'))
    assert dispatch.retry_failed()==0
    dispatch.update(second,'rejected')
    assert dispatch.retry_failed()==1
    assert dispatch.active('live','correctness')['id']==first
    assert store.rows('SELECT state FROM repair_requests WHERE id=?',(second,))[0]['state']=='rejected'


def test_evaluator_revision_does_not_bypass_pending_metric_lock(monkeypatch):
    first=dispatch.schedule(incident('a'))
    dispatch.update(first,'awaiting_review')
    monkeypatch.setattr(dispatch,'evaluator_version',lambda:'new-judge')
    store.set_setting('evaluator_audit',{'status':'passed','evaluator_version':'new-judge'})
    newer=incident('b')
    payload=json.loads(newer['payload'])
    payload['evaluator_version']='new-judge'
    newer['payload']=json.dumps(payload)
    assert dispatch.schedule(newer)==first
    assert len(store.rows('SELECT * FROM repair_requests'))==1


def test_recovery_and_rebreach_preserve_pending_pr_evidence(monkeypatch):
    from quality import monitoring
    def rows(label):
        return [{'id':str(i),'trace_id':str(i),'evaluation':{'metrics':{'correctness':{'label':label}}}} for i in range(20)]
    monkeypatch.setattr(monitoring,'current_window',lambda *a,**kw:rows('fail'))
    monitoring.monitor('live',agent_version())
    first=store.rows('SELECT * FROM incidents')[0]
    original=json.loads(first['payload'])
    request=dispatch.active('live','correctness')
    dispatch.update(request['id'],'awaiting_review')
    monkeypatch.setattr(monitoring,'current_window',lambda *a,**kw:rows('pass'))
    monitoring.monitor('live',agent_version())
    assert store.rows('SELECT status FROM incidents')[0]['status']=='resolved'
    monkeypatch.setattr(monitoring,'current_window',lambda *a,**kw:rows('fail'))
    monitoring.monitor('live',agent_version())
    restored=json.loads(store.rows('SELECT payload FROM incidents')[0]['payload'])
    assert restored['episode_id']==original['episode_id']
    assert restored['window_run_ids']==original['window_run_ids']
    assert len(store.rows('SELECT * FROM repair_requests'))==1
    assert len(store.rows("SELECT * FROM jobs WHERE kind='email' AND key LIKE 'incident:%'"))==1


def test_old_evaluator_event_cannot_launch_monitor(monkeypatch):
    from quality import monitoring
    called=[]
    monkeypatch.setattr(monitoring,'monitor',lambda *a,**kw:called.append(kw))
    monitoring.evaluation_ready({'source':'live','version':agent_version(),'evaluation_version':'retired'})
    assert not called
    monitoring.evaluation_ready({'source':'live','version':agent_version(),'evaluation_version':evaluator_version()})
    assert len(called)==1


def test_delayed_monitor_uses_breach_snapshot_instead_of_later_recovery(monkeypatch):
    from quality import monitoring
    def window(label):
        return [{'id':str(i),'trace_id':str(i),'evaluation':{'metrics':{'correctness':{'label':label}}}} for i in range(20)]
    monkeypatch.setattr(monitoring,'current_window',lambda *a,**kw:window('pass'))
    monitoring.evaluation_ready({'source':'live','version':agent_version(),'evaluation_version':evaluator_version(),
                                 'window_snapshot':window('fail')})
    assert len(store.rows('SELECT * FROM incidents'))==1
    assert dispatch.active('live','correctness') is not None


def test_signed_pr_event_and_confirmed_close_release_lock(monkeypatch):
    from fastapi.testclient import TestClient
    from agent.api import app
    from quality import remediation
    pr_url='https://github.com/example/repo/pull/1'
    identifier=dispatch.schedule(incident('a'))
    store.execute("UPDATE repair_requests SET state='awaiting_review',detail=? WHERE id=?",(json.dumps({'pr_url':pr_url}),identifier))
    monkeypatch.setenv('QUALITY_GITHUB_WEBHOOK_SECRET','isolated-test-webhook-secret')
    body=json.dumps({'action':'closed','pull_request':{'html_url':pr_url}}).encode()
    headers={'x-github-event':'pull_request','x-github-delivery':'delivery-1','x-hub-signature-256':'invalid'}
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        assert client.post('/quality/github/webhook',content=body,headers=headers).status_code==401
        headers['x-hub-signature-256']='sha256='+hmac.new(b'isolated-test-webhook-secret',body,hashlib.sha256).hexdigest()
        assert client.post('/quality/github/webhook',content=body,headers=headers).status_code==202
        assert client.post('/quality/github/webhook',content=body,headers=headers).status_code==202
    assert len(store.rows("SELECT * FROM jobs WHERE kind='pr_lifecycle'"))==1
    monkeypatch.setattr(remediation,'credential',lambda:'')
    state={'html_url':pr_url,'state':'open'}
    monkeypatch.setattr('httpx.get',lambda *a,**kw:SimpleNamespace(raise_for_status=lambda:None,json=lambda:state))
    dispatch.sync_pr(pr_url)
    assert dispatch.active('live','correctness')
    state.update(state='closed',merged=True,closed_at='2026-09-16T00:00:00Z')
    dispatch.sync_pr(pr_url)
    assert dispatch.active('live','correctness') is None
    assert store.rows('SELECT state FROM repair_requests')[0]['state']=='merged'


def test_raw_weather_seeds_are_not_judge_evidence():
    from quality.profiles.travel import evaluation_catalog,reference_tool
    weather=evaluation_catalog()['weather']
    assert 'Chicago' in weather['supported_cities'] and 'high_f' not in json.dumps(weather)
    assert reference_tool('get_weather',{'city':'Chicago','date':'2026-10-05'})['high_f']==60
    assert reference_tool('get_weather',{'city':'Tokyo','date':'2026-10-02'})['low_f']==60
