const $=s=>document.querySelector(s);
const names={correctness:'Correctness',groundedness:'Groundedness',topic_relevance:'Topic relevance',task_completion:'Task completion',checkpoint_contract:'Tool and evaluation checks',privacy:'Privacy'};
const primary=['correctness','groundedness','topic_relevance'];
const sourceName=source=>source==='scenario'?'Archived simulation':source==='live'?'Live':source;
const labels={merged:'Merged',pr_open:'Draft PR',pr_closed:'PR closed',awaiting_repair:'Validating fix',not_applicable:'N/A'};
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const badge=(text,kind=text)=>el('span',labels[text]||text.replaceAll('_',' '),'badge '+kind);
const pct=x=>x==null?'—':(x*100).toFixed(1)+'%';
const dt=x=>x?new Date(x*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const empty=(n,text)=>n.append(el('div',text,'empty'));
const link=(label,url)=>{const a=el('a',label);if(/^https?:\/\//.test(url||'')){a.href=url;a.target='_blank';a.rel='noopener'}return a};
const action=(label,fn)=>{const b=el('button',label);b.onclick=fn;return b};
let historyConversation=null,historyBenchmark=null,historyAgent=null,historyEvaluation=null;
let liveSelection='latest';try{liveSelection=localStorage.getItem('quality-live-view')||'latest'}catch{}
let snapshot,scope='live',activityLimit=8,selectedRun,historyCursor=null,historyStack=[],nextCursor=null,historySequence=0,refreshing=false,evaluationSubmitting=false,evaluationRequest=null,evaluationError='';
async function get(url){const r=await fetch(url);if(!r.ok)throw Error('Could not load data ('+r.status+')');return r.json()}
function benchmarkOptions(){const n=$('#benchmark'),old=n.value,items=[...snapshot.benchmarks].reverse();const key=items.map(b=>b.id+':'+b.status).join();if(n.dataset.key===key)return;n.dataset.key=key;n.replaceChildren();items.forEach(b=>n.add(new Option(`${b.kind} · ${(b.saved_version?.version||b.current_version).slice(0,8)} · ${dt(b.created)} · ${b.status}${b.requires_revalidation?' · old evaluator':''}`,b.id)));if(items.some(b=>b.id===old))n.value=old;else n.value=(items.find(b=>!b.requires_revalidation)||items.find(b=>b.status==='complete')||items[0])?.id||''}
function renderFullEvaluation(){
 const s=snapshot,f=s.full_evaluation,b=$('#full-evaluation'),latest=f?.latest;
 const targets=s.checkpoints?.targets||[],select=$('#evaluation-version'),old=select.value,key=targets.map(t=>t.id+':'+t.version).join();
 if(select.dataset.key!==key){select.dataset.key=key;select.replaceChildren(new Option('Running agent · '+s.serving_agent.version.slice(0,8),'running'));targets.filter(t=>t.id!=='running').forEach(t=>select.add(new Option(t.label+' · '+t.version.slice(0,8),t.id)));if([...select.options].some(o=>o.value===old))select.value=old}
 const target=targets.find(t=>t.id===select.value),version=target?.version||s.serving_agent.version;
 b.disabled=evaluationSubmitting||f?.busy||s.serving_agent.restart_required||target?.state==='stale_configuration'||Boolean(s.provider_block);
 b.textContent=evaluationSubmitting?'Queuing…':f?.busy?'Evaluation / repair in progress':'Run full evaluation';
 $('#evaluation-target').textContent=`${target?.label||'Running agent'} ${version.slice(0,12)} · 60 scenarios × ${f?.repetitions||2} runs · Uses model credits.`;
 $('#cadence-status').textContent=s.checkpoints?.enabled?`Daily checkpoints enabled · 24 hours AND a changed version. ${target?.state==='up_to_date'?'This version already has a full evaluation.':target?.state==='waiting'?'Next eligible: '+dt(target.due_at)+'.':target?.state==='needs_attention'?'Last checkpoint needs operator attention.':target?.state||''}`:'Daily checkpoints paused.';
 const requestBenchmark=s.benchmarks.find(x=>x.id===latest?.id);
 const progress=requestBenchmark?` ${requestBenchmark.progress.evaluated||0}/${requestBenchmark.progress.total} recorded responses evaluated.`:'';
 $('#evaluation-status').textContent=evaluationError||(s.serving_agent.restart_required?'Restart the app and worker to evaluate changed agent code.':s.provider_block?'Resolve the provider block before starting an evaluation.':latest?latest.state==='done'?'Full evaluation saved as version '+latest.id.slice(0,8)+'.':latest.state==='dead'?`Full evaluation stopped (${latest.error}). Partial evidence is preserved; inspect the worker log before retrying.`:`Full evaluation ${latest.state==='pending'?'queued':latest.state}.${progress}`:'Each run saves a separate version. The original baseline remains available.');
 $('#evaluation-status').className=evaluationError||latest?.state==='dead'?'small error':'small';
}
async function startFullEvaluation(){
 if(evaluationSubmitting||$('#full-evaluation').disabled)return;
 const targetId=$('#evaluation-version').value,target=snapshot.checkpoints?.targets.find(t=>t.id===targetId),version=target?.version||snapshot.serving_agent.version;
 if(!evaluationRequest||evaluationRequest.version!==version||evaluationRequest.targetId!==targetId)evaluationRequest={id:crypto.randomUUID(),version,targetId};
 evaluationSubmitting=true;evaluationError='';renderFullEvaluation();
 try{
  const r=await fetch('/quality/benchmarks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:evaluationRequest.id,expected_version:version,target_id:targetId})});
  const data=await r.json();if(!r.ok){evaluationRequest=null;throw Error(data.detail||'Could not queue evaluation')}
  evaluationRequest=null;await refresh();if(snapshot.benchmarks.some(b=>b.id===data.id))$('#benchmark').value=data.id;
 }catch(e){evaluationError=e.message}finally{evaluationSubmitting=false;renderMetrics()}
}
function selectLive(value){liveSelection=value;try{localStorage.setItem('quality-live-view',value)}catch{}renderMetrics()}
function renderLiveHistory(){
 const cohorts=snapshot.live_history?.cohorts||[],select=$('#live-version');
 if(!['latest','current'].includes(liveSelection)&&!cohorts.some(c=>c.id===liveSelection))liveSelection='latest';
 const key=cohorts.map(c=>c.id).join();if(select.dataset.key!==key){select.dataset.key=key;select.replaceChildren(new Option('Latest recorded performance','latest'),new Option('Current warning window · last 24 hours','current'));cohorts.forEach(c=>select.add(new Option(`Agent ${c.version.slice(0,8)} · Evaluator ${c.evaluator_version.slice(0,8)}`,c.id)))}select.value=liveSelection;
 const n=$('#live-cohorts');n.replaceChildren();
 cohorts.forEach(c=>{const row=el('tr'),version=el('td');version.append(el('div','Agent '+c.version.slice(0,8)),el('div',`Evaluator ${c.evaluator_version.slice(0,8)} · ${c.conversation_count} conversations recorded`,'small'));row.append(version);primary.forEach(key=>{const m=c.report.metrics[key];row.append(el('td',`${pct(m.pass_rate)} (${m.pass}/${m.n})`))});const actions=el('td');actions.append(action('Show metrics',()=>selectLive(c.id)),action('Traces',()=>filterHistory({agent:c.version,evaluation:c.evaluator_version})));row.append(el('td',dt(c.last_seen)),actions);n.append(row)});
 if(!cohorts.length){const row=el('tr'),cell=el('td','No live conversations recorded yet.','empty');cell.colSpan=6;row.append(cell);n.append(row)}
 $('#live-window-note').textContent=`Current warning window: ${snapshot.live.total}/${snapshot.monitor.window} conversations in 24 hours · Agent ${snapshot.serving_agent.version.slice(0,8)} · Evaluator ${snapshot.evaluator_version.slice(0,8)}${snapshot.live_status!=='connected'?' · '+snapshot.live_status:''}. Recorded history does not expire when this window resets.`;
 return liveSelection==='current'?null:liveSelection==='latest'?cohorts[0]:cohorts.find(c=>c.id===liveSelection);
}
function renderMetrics(){
 const s=snapshot,offline=scope==='benchmark',b=offline?s.benchmarks.find(x=>x.id===$('#benchmark').value):null;
 document.querySelectorAll('[data-scope]').forEach(n=>n.setAttribute('aria-pressed',String(n.dataset.scope===scope)));
 $('#benchmark-controls').hidden=!offline;$('#experiment-link').hidden=!b;if(b)$('#experiment-link').href=b.experiment_url;
 $('#version-link').hidden=!b?.saved_version;if(b?.saved_version)$('#version-link').href='/quality/benchmarks/'+b.id+'/version';renderFullEvaluation();
 const checkpoint=s.checkpoints?.decisions.find(d=>d.benchmark_id===b?.id);$('#checkpoint-review').hidden=!checkpoint;$('#checkpoint-review').onclick=()=>showCheckpoint(checkpoint);
 const cohort=renderLiveHistory(),recorded=!offline&&liveSelection!=='current';$('#live-history').hidden=offline;
 const report=offline?(b?.report||b?.current):recorded?cohort?.report:s.live;
 const base=b?.parent_id&&b?.manifest.suite!=='targeted'?s.benchmarks.find(x=>x.id===b.parent_id):null;
 $('#scope-note').textContent=offline?(b?`${b.kind==='baseline'?'Saved baseline':b.kind==='checkpoint'?'Full checkpoint':'Candidate validation'} · ${b.progress.evaluated||0}/${b.progress.total} responses evaluated · ${b.requires_revalidation?'Historical scores from a previous evaluator; revalidation required.':b.status==='complete'?`${b.report.total} final scenario outcomes; sealed experiment.`: 'Results are provisional until the experiment completes.'}`:'No benchmark recorded yet.'):recorded?(cohort?`Recorded live performance · Agent ${cohort.version.slice(0,8)} · Evaluator ${cohort.evaluator_version.slice(0,8)} · Latest ${report.total} of ${cohort.conversation_count} conversations · Last activity ${dt(cohort.last_seen)}${!cohort.current_evaluator?' · Previous evaluator':''}`:'No live conversations recorded yet.'):`Current warning window · ${report?.total||0} of ${s.monitor.window} conversations · Latest response per conversation within 24 hours.${s.live_status!=='connected'?' '+s.live_status:''}`;
 const n=$('#metrics');n.replaceChildren();
 Object.entries(names).filter(([key])=>primary.includes(key)).forEach(([key,title])=>{const m=report?.metrics?.[key],warm=!offline&&!recorded&&(!m||m.n<s.monitor.minimum_samples),warn=!warm&&m?.pass_rate!=null&&m.pass_rate<s.monitor.threshold;
 const c=el('article',undefined,'card'),head=el('div',undefined,'card-title');head.append(el('span',title),badge(offline?(b?.requires_revalidation?'historical':b?.status||'pending'):recorded?'recorded':warm?'warming up':warn?'below '+Math.round(s.monitor.threshold*100)+'%':'healthy',recorded?'historical':warn?'fail':warm?'pending':'pass'));c.append(head,el('div',pct(m?.pass_rate),'value'+(warn?' warn':'')));
 c.append(el('div',m?`${m.pass} pass / ${m.n} applicable`:'No evaluations yet','small'));const bar=el('div',undefined,'bar'+(warn?' warn':'')),fill=el('span');fill.style.width=((m?.pass_rate||0)*100)+'%';bar.append(fill);c.append(bar);
 c.append(el('div',`${m?.pending||0} pending · ${m?.unknown||0} unknown · ${m?.not_applicable||0} N/A`,'small'));
 if(warm)c.append(el('div',`Needs ${s.monitor.minimum_samples} applicable results to warn`,'small'));
 const bm=(base?.report||base?.current)?.metrics?.[key];if(b?.status==='complete'&&base?.status==='complete'&&bm&&m?.pass_rate!=null&&bm.pass_rate!=null){const delta=(m.pass_rate-bm.pass_rate)*100;c.append(el('div',`Baseline ${pct(bm.pass_rate)} → ${delta>=0?'+':''}${delta.toFixed(1)} pp`,'small'))}
 c.append(action('Inspect failures',()=>filterHistory({source:offline?'all':scope,metric:key,label:'fail',benchmark:b?.id,agent:offline?null:recorded?cohort?.version:s.serving_agent.version,evaluation:offline?null:recorded?cohort?.evaluator_version:s.evaluator_version})));n.append(c)});
 $('#policy').textContent=`${s.monitor.window} conversations · Warn below ${Math.round(s.monitor.threshold*100)}%`;
 $('#method-policy').textContent=`Recorded live performance retains the latest ${s.monitor.window} distinct conversations for every agent and evaluator version with no time cutoff. It survives restarts and deployments. Automatic warnings separately use the current version's Phoenix window within 24 hours. Each metric needs ${s.monitor.minimum_samples} applicable results. Below ${pct(s.monitor.threshold)} triggers a warning and validated fix proposal; recovery is ${pct(s.monitor.recovery_threshold)}. Every response is evaluated; rolling scores use the latest response per conversation. All earlier turns and previous judgments remain in trace history. Completion is diagnostic. Benchmarks remain separate from live traffic.`;
}
function renderActivity(){
 const s=snapshot;
 const incident=s.incidents.find(i=>i.status==='open'&&i.payload.source==='live'&&i.payload.version===s.serving_agent.version&&i.payload.evaluator_version===s.evaluator_version);
 const baseline=s.benchmarks.find(b=>b.id===incident?.payload.validation_baseline_id);
 const repair=incident?s.repairs.find(r=>r.incident_id===incident.id):null;
 const currentExperiment=s.benchmarks.find(b=>b.id===(repair?.payload.targeted_benchmark_id||repair?.payload.candidate_benchmark_id))||baseline;
 const progress=currentExperiment?` ${currentExperiment.kind==='baseline'?'Current-agent baseline':'Candidate experiment'}: ${currentExperiment.progress.evaluated||0}/${currentExperiment.progress.total} responses evaluated (${currentExperiment.status}).`:'';
 const requests=(s.repair_requests||[]).filter(r=>['queued','waiting_baseline','running','awaiting_review','needs_attention'].includes(r.state));
 const stages=requests.map(r=>{const b=s.benchmarks.find(b=>b.id===r.baseline_id);return `${names[r.metric]||r.metric}: ${r.state.replaceAll('_',' ')}${r.state==='waiting_baseline'&&b?` (${b.progress.evaluated||0}/${b.progress.total} baseline turns evaluated)`:''}`}).join(' · ');
 $('#workflow').textContent=s.provider_block?s.provider_block.message:stages|| (incident?`Live escalation · ${names[incident.payload.metric]||incident.payload.metric}.${repair?' '+(labels[repair.status]||repair.status).replaceAll('_',' ')+'.':''}${progress}`:'Evaluation events trigger monitoring. Different metrics can have concurrent fixes; each metric stays locked through PR review.');
 const filter=$('#activity-filter').value,all=[...s.incidents.map(x=>({...x,kind:'incident'})),...s.repairs.map(x=>({...x,kind:'repair'})),...(s.email_events||[]).map(x=>({...x,kind:'email'}))].filter(x=>filter==='all'||x.kind===filter||filter==='incident'&&x.kind==='email').sort((a,b)=>b.created-a.created);
 const n=$('#activity');n.replaceChildren();if(!all.length)empty(n,'No escalations or proposed fixes yet.');all.slice(0,activityLimit).forEach(x=>{const row=el('div',undefined,'activity-row'),desc=el('div');
 if(x.kind==='email'){const incident=s.incidents.find(i=>i.id===x.incident_id);desc.append(el('div',`${names[incident?.payload.metric]||'Quality'} email${x.event?' · '+x.event.replaceAll('_',' '):''}`,'title'),el('div',emailSummary(x),'small'));row.append(badge('Email '+x.status,x.status==='sent'||x.status==='received'?'pass':x.status==='failed'?'fail':'pending'),desc,el('time',dt(x.created),'small'),action('View email ↗',()=>showEmail(x)));n.append(row);return}
 const title=x.kind==='repair'?(x.payload.pr_url?'PR #'+x.payload.pr_url.split('/').pop()+' · '+(names[x.payload.metric]||'Agent improvement'):(names[x.payload.metric]||'Agent improvement')+' · '+x.id.slice(0,8)):x.payload.trigger==='operator_review'?`${names[x.payload.metric]||x.payload.metric} · Review requested`:x.payload.source==='checkpoint'?`${names[x.payload.metric]||x.payload.metric} · Checkpoint regression`:`${names[x.payload.metric]||x.payload.metric} below ${pct(x.payload.threshold)}`;
 desc.append(el('div',title,'title'),el('div',`${x.kind==='repair'?'Proposed fix':x.payload.trigger==='operator_review'?'Operator-requested review':sourceName(x.payload.source)+' conversation window'}${x.requires_revalidation?' · Previous evaluator':''}`,'small'));
 if(x.kind==='incident'){const email=(s.email_events||[]).find(e=>e.incident_id===x.id);desc.append(email?action('Email '+email.status+' · View receipt',()=>showEmail(email)):el('div','No email event recorded','small'))}
 row.append(badge(x.status),desc,el('time',dt(x.created),'small'),action('Details ↗',()=>showActivity(x)));n.append(row)});
 $('#more-activity').hidden=all.length<=activityLimit;
 $('#active-versions').textContent=`Running agent ${s.serving_agent.version.slice(0,12)} · Evaluator ${s.evaluator_version.slice(0,12)}. Candidate PRs do not change the running agent.`;
 const key=JSON.stringify(s.version_updates),v=$('#versions');if(v.dataset.key!==key){v.dataset.key=key;v.replaceChildren();s.version_updates.forEach(x=>{const d=el('details',undefined,'item');const summary=el('summary',`${x.kind} ${x.version?.slice(0,12)||'pending'} · ${dt(x.at)} · `);summary.append(badge(x.status));d.append(summary,el('p',x.summary),el('p',x.detail,'small'));if(x.pr_url)d.append(link('Review PR ↗',x.pr_url));v.append(d)})}
}
function openDetail(title,review=false){selectedRun=null;$('#detail-title').textContent=title;$('#review-panel').hidden=!review;$('#review-panel').open=false;$('#review').reset();$('#review-status').textContent='';const n=$('#detail-body');n.replaceChildren();if(!$('#detail').open)$('#detail').showModal();return n}
function rawDetails(n,title,value){const d=el('details');d.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));n.append(d)}
function emailSummary(e){return [e.transport,e.smtp_code?'SMTP '+e.smtp_code:null,e.recipient?'To: '+e.recipient:null,!e.accepted&&e.error_type?e.error_type:null,!e.accepted&&e.attempts!=null?e.attempts+' attempts':null].filter(Boolean).join(' · ')||'Awaiting a delivery receipt'}
function emailReceipt(n,e){
 n.append(badge('Email '+e.status,e.status==='sent'||e.status==='received'?'pass':e.status==='failed'?'fail':'pending'),el('p',emailSummary(e)),el('p',`Queued: ${dt(e.queued_at)} · SMTP accepted: ${dt(e.accepted)} · Inbox received: ${dt(e.received)}`,'small'));
 n.append(el('p',e.transport==='local SMTP inbox'?'Delivered to the local demo inbox; no external mailbox delivery is claimed.':e.accepted?'The configured SMTP relay accepted this message. Destination mailbox delivery is not verified.':'A successful send has not been confirmed.','small'));
 if(e.subject)n.append(el('p','Subject: '+e.subject));n.append(el('p','Message-ID: '+e.id,'small version-id'));if(e.smtp_response)n.append(el('p','SMTP response: '+e.smtp_response,'small'));if(e.error_type&&!e.accepted)n.append(el('p',`Last attempt: ${e.error_type} · ${e.attempts} attempts`,'error'));if(e.body)n.append(el('pre',e.body));
}
function showEmail(e){const n=openDetail('Email delivery');emailReceipt(n,e);const incident=snapshot.incidents.find(i=>i.id===e.incident_id);if(incident)n.append(action('View related escalation',()=>showActivity({...incident,kind:'incident'})))}
function showActivity(x){
 const n=openDetail(x.kind==='repair'?'Proposed fix':'Escalation'),p=x.payload;n.append(badge(x.status),el('p',dt(x.created),'small'),el('p',p.summary||p.description||''));if(x.requires_revalidation)n.append(el('p','Historical evidence from a previous evaluator. It does not establish current performance.','error'));
 if(p.pr_url)n.append(link(x.status==='merged'?'View merged PR ↗':x.status==='pr_closed'?'View closed PR ↗':'Review draft PR ↗',p.pr_url));if(p.closed_reason)n.append(el('p',p.closed_reason));
 if(p.measurement)n.append(el('p',`${names[p.metric]}: ${pct(p.measurement.pass_rate)} (${p.measurement.pass}/${p.measurement.n} applicable), warning threshold ${pct(p.threshold)}.`));
 const incidentId=x.kind==='incident'?x.id:x.incident_id,incident=snapshot.incidents.find(i=>i.id===incidentId);
 const request=(snapshot.repair_requests||[]).find(r=>r.incident_id===incidentId);if(request){n.append(el('h3','Repair workflow'),badge(request.state),el('p',`Metric: ${names[request.metric]||request.metric}. This metric cannot start another fix while this investigation or its PR remains active.`));if(request.detail.reason)n.append(el('p',request.detail.reason));if(request.baseline_id){const base=snapshot.benchmarks.find(b=>b.id===request.baseline_id);if(base)n.append(link('Measured shared baseline ↗',base.experiment_url))}}
 const ids=(incident?.payload||p).failing_run_ids||[];if(ids.length){n.append(el('h3','Triggering evidence'));const box=el('div');ids.forEach((id,i)=>box.append(action('Failed response '+(i+1),()=>inspect(id))));n.append(box)}
 ['baseline_id','validation_baseline_id','candidate_benchmark_id','targeted_benchmark_id','benchmark_id'].forEach(key=>{const b=snapshot.benchmarks.find(b=>b.id===p[key]);if(b){n.append(el('h3',key==='targeted_benchmark_id'?'Targeted development check':key==='candidate_benchmark_id'||key==='benchmark_id'?'Candidate / checkpoint':'Before: baseline'),link('Open measured Phoenix experiment ↗',b.experiment_url));if(b.report)rawDetails(n,'Experiment results',b.report)}});
 const emails=(snapshot.email_events||[]).filter(e=>e.incident_id===incidentId);n.append(el('h3','Email delivery'));if(!emails.length)n.append(el('p','No email event is recorded for this escalation.','muted'));
 emails.forEach(e=>{const box=el('div',undefined,'email-receipt');emailReceipt(box,e);n.append(box)});rawDetails(n,'Recorded workflow evidence',p);
}
async function inspect(id,evaluationVersion=null){
 const n=openDetail('Execution details',true);selectedRun=id;n.append(el('p','Loading execution and Phoenix trace…'));
 try{const data=await get('/quality/runs/'+encodeURIComponent(id)+(evaluationVersion?'?evaluation_version='+encodeURIComponent(evaluationVersion):''));if(selectedRun!==id)return;n.replaceChildren(el('p','Trace '+data.event.trace_id,'small version-id'),el('p',`${dt(data.created)} · Agent ${data.event.version.slice(0,12)} · Evaluator ${data.evaluation?.version?.slice(0,12)||'pending'} · ${data.source}`,'small'));
 n.append(action('View all conversation turns',()=>filterHistory({source:data.source,conversation:data.event.conversation_id||id})),el('h3','Response for this turn'),el('pre',data.event.output));const scores=el('div');Object.entries(names).forEach(([key,title])=>{const m=data.evaluation?.metrics?.[key],d=el('details'),summary=el('summary',title+(primary.includes(key)?'':' (diagnostic)')+' · ');summary.append(badge(m?.label||'pending'));d.append(summary,el('p',m?.explanation||'Evaluation pending.'));if(m?.annotator)d.append(el('p','Scored by '+m.annotator,'small'));scores.append(d)});n.append(el('h3','Evaluations'),scores);if(data.evaluation?.version!==snapshot.evaluator_version)n.append(el('p','Evaluation is pending or uses a previous evaluator.','small'));
 renderTrace(data.trace,n);rawDetails(n,'Conversation input',data.event.input);rawDetails(n,'Tool diagnostics & evaluation evidence',data.evaluation);rawDetails(n,'Previous evaluations (preserved)',data.evaluation_history||[]);
 }catch(e){if(selectedRun===id)n.replaceChildren(el('p',e.message,'error'))}
}
function filterHistory({source='live',metric='',label='',conversation=null,benchmark=null,agent=null,evaluation=null}={}){
 historyConversation=conversation;historyBenchmark=benchmark;historyAgent=agent;historyEvaluation=evaluation;historyCursor=null;historyStack=[];
 $('#trace-source').value=['live','benchmark','all'].includes(source)?source:'all';$('#trace-metric').value=metric;$('#trace-label').value=label;$('#trace-evaluator').value='all';
 if($('#detail').open)$('#detail').close();loadHistory();$('#trace-section').scrollIntoView({behavior:'smooth',block:'start'});
}
function resetHistoryPage(){historyCursor=null;historyStack=[];loadHistory()}
async function loadHistory(){
 const seq=++historySequence,q=new URLSearchParams({source:$('#trace-source').value,limit:'12',evaluator:$('#trace-evaluator').value});
 for(const [param,id] of [['metric','trace-metric'],['label','trace-label']])if($('#'+id).value)q.set(param,$('#'+id).value);
 if(historyCursor)q.set('cursor',historyCursor);if(historyConversation)q.set('conversation_id',historyConversation);if(historyBenchmark)q.set('benchmark_id',historyBenchmark);
 if(historyAgent)q.set('agent_version',historyAgent);if(historyEvaluation)q.set('evaluation_version',historyEvaluation);
 $('#trace-context').textContent=[historyConversation?'Conversation '+historyConversation:null,historyBenchmark?'Experiment '+historyBenchmark:null,historyAgent?'Agent '+historyAgent.slice(0,12):null,historyEvaluation?'Evaluator '+historyEvaluation.slice(0,12):null].filter(Boolean).join(' · ');
 $('#clear-trace-context').hidden=!historyConversation&&!historyBenchmark&&!historyAgent&&!historyEvaluation;
 try{
  const h=await get('/quality/runs?'+q);if(seq!==historySequence)return;nextCursor=h.next_cursor;const n=$('#traces');n.replaceChildren();
  h.items.forEach(x=>{
   const row=el('tr'),question=el('td');question.append(el('div',x.question,'question'),el('div',x.trace_id+' · Agent '+x.version.slice(0,8),'trace-id'),el('div',`${sourceName(x.source)} · ${dt(x.created)}`,'small compact-only'));
   const qualityCell=el('td');qualityCell.className='trace-quality';
   const selected=$('#trace-metric').value,keys=selected&&!primary.includes(selected)?[...primary,selected]:primary;
   if(x.requires_revalidation)qualityCell.append(el('div','Previous evaluator','small'));
   keys.forEach(key=>{const label=x.metrics?.[key]?.label||'pending';qualityCell.append(badge(names[key]+': '+(labels[label]||label.replaceAll('_',' ')),label))});
   const detail=el('td');detail.append(action('Inspect ↗',()=>inspect(x.id,x.evaluator_version)),action('Conversation',()=>filterHistory({source:x.source,conversation:x.conversation_id,evaluation:historyEvaluation})));
   row.append(question,el('td',sourceName(x.source)),qualityCell,el('td',String(x.tool_count)),el('td',dt(x.created),'small'),detail);n.append(row);
  });
  if(!h.items.length){const row=el('tr'),cell=el('td','No recorded responses match these filters.','empty');cell.colSpan=6;row.append(cell);n.append(row)}
  $('#trace-count').textContent=`${h.total} matching responses · Page ${historyStack.length+1}`;$('#older').disabled=!nextCursor;$('#newer').disabled=!historyStack.length;
 }catch(e){$('#trace-count').textContent=e.message}
}
async function refresh(){if(refreshing)return;refreshing=true;try{snapshot=await get('/quality/state');benchmarkOptions();renderMetrics();renderActivity();$('#connection').textContent='Updated '+new Date().toLocaleTimeString();$('#connection').className='';if(!historyCursor)await loadHistory()}catch(e){$('#connection').textContent=e.message;$('#connection').className='error'}finally{refreshing=false}}
document.querySelectorAll('[data-scope]').forEach(n=>n.onclick=()=>{scope=n.dataset.scope;if(snapshot)renderMetrics()});$('#benchmark').onchange=renderMetrics;$('#live-version').onchange=e=>selectLive(e.target.value);$('#activity-filter').onchange=()=>{activityLimit=8;renderActivity()};$('#more-activity').onclick=()=>{activityLimit+=10;renderActivity()};['trace-source','trace-metric','trace-label'].forEach(id=>$('#'+id).onchange=resetHistoryPage);$('#trace-evaluator').onchange=()=>{historyEvaluation=null;resetHistoryPage()};$('#clear-trace-context').onclick=()=>{historyConversation=null;historyBenchmark=null;historyAgent=null;historyEvaluation=null;resetHistoryPage()};$('#older').onclick=()=>{historyStack.push(historyCursor);historyCursor=nextCursor;loadHistory()};$('#newer').onclick=()=>{historyCursor=historyStack.pop()||null;loadHistory()};$('#close').onclick=()=>{$('#detail').close();selectedRun=null};$('#detail').addEventListener('close',()=>selectedRun=null);
Object.entries(names).forEach(([key,title])=>{const l=el('label',title),s=el('select');s.name=key;['unknown','pass','fail','not_applicable'].forEach(x=>s.add(new Option(x.replaceAll('_',' '),x)));l.append(s);$('#review-labels').append(l)});
$('#review').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/quality/runs/'+selectedRun+'/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});$('#review-status').textContent=r.ok?' Saved.':' Could not save.'}catch{$('#review-status').textContent=' Could not save.'}};
$('#full-evaluation').onclick=startFullEvaluation;
$('#evaluation-version').onchange=renderFullEvaluation;
refresh();setInterval(refresh,5000);

function showCheckpoint(d){
 const n=openDetail('Full checkpoint review');n.append(badge(d.conclusion),el('p','Version '+d.version.slice(0,12)+'. Approval records a quality decision; deployment remains a separate human action.'));
 Object.entries(d.comparisons).forEach(([name,c])=>{n.append(el('h3',name==='original'?'Against original baseline':'Against last approved checkpoint'));Object.entries(c.overall.metrics).forEach(([metric,m])=>n.append(el('p',`${names[metric]}: ${pct(m.before)} → ${pct(m.after)} · ${m.conclusion} · ${m.paired_scenarios} paired scenarios.`)));rawDetails(n,'Paired comparisons and held-out results',c)});
 d.blockers.forEach(text=>n.append(el('p',text,'error')));
 if(snapshot.checkpoints.approved.includes(d.benchmark_id)){n.append(el('p','This checkpoint has a recorded human approval.'));return}
 if(d.blockers.length||d.conclusion==='regressed'){n.append(el('p','Approval is blocked until the regression or contract failure is addressed.'));return}
 const form=el('form'),label=el('label','Review note'),note=el('textarea');note.name='note';note.maxLength=2000;label.append(note);form.append(label);
 const accept=el('input');accept.type='checkbox';const ack=el('label','I accept the uncertainty in this checkpoint.');ack.prepend(accept);form.append(ack);if(d.conclusion==='inconclusive'){note.required=true;accept.required=true}
 const button=el('button','Approve this evaluated version');button.type='submit';const status=el('p');status.setAttribute('role','status');form.append(button,status);n.append(form);
 form.onsubmit=async e=>{e.preventDefault();button.disabled=true;try{const r=await fetch('/quality/benchmarks/'+d.benchmark_id+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_version:d.version,note:note.value,accept_uncertainty:accept.checked})});const data=await r.json();if(!r.ok)throw Error(data.detail||'Approval failed');status.textContent='Approval saved. No deployment performed.';await refresh()}catch(error){status.textContent=error.message;button.disabled=false}};
}
