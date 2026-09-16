const $=s=>document.querySelector(s);
const names={correctness:'Correctness',groundedness:'Groundedness',topic_relevance:'Topic relevance',task_completion:'Task completion'};
const labels={pr_open:'Draft PR',pr_closed:'PR closed',awaiting_repair:'Validating fix',not_applicable:'N/A'};
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const badge=(text,kind=text)=>el('span',labels[text]||text.replaceAll('_',' '),'badge '+kind);
const pct=x=>x==null?'—':(x*100).toFixed(1)+'%';
const dt=x=>x?new Date(x*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const empty=(n,text)=>n.append(el('div',text,'empty'));
const link=(label,url)=>{const a=el('a',label);if(/^https?:\/\//.test(url||'')){a.href=url;a.target='_blank';a.rel='noopener'}return a};
const action=(label,fn)=>{const b=el('button',label);b.onclick=fn;return b};
let snapshot,scope='live',activityLimit=6,selectedRun,historyCursor=null,historyStack=[],nextCursor=null,historySequence=0,refreshing=false,evaluationSubmitting=false,evaluationRequest=null,evaluationError='';
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
function renderMetrics(){
 const s=snapshot,offline=scope==='benchmark',b=offline?s.benchmarks.find(x=>x.id===$('#benchmark').value):null;
 document.querySelectorAll('[data-scope]').forEach(n=>n.setAttribute('aria-pressed',String(n.dataset.scope===scope)));
 $('#benchmark-controls').hidden=!offline;$('#experiment-link').hidden=!b;if(b)$('#experiment-link').href=b.experiment_url;
 $('#version-link').hidden=!b?.saved_version;if(b?.saved_version)$('#version-link').href='/quality/benchmarks/'+b.id+'/version';renderFullEvaluation();
 const checkpoint=s.checkpoints?.decisions.find(d=>d.benchmark_id===b?.id);$('#checkpoint-review').hidden=!checkpoint;$('#checkpoint-review').onclick=()=>showCheckpoint(checkpoint);
 const report=offline?(b?.report||b?.current):s[scope];
 const base=b?.parent_id&&b?.manifest.suite!=='targeted'?s.benchmarks.find(x=>x.id===b.parent_id):null;
 $('#scope-note').textContent=offline?(b?`${b.kind==='baseline'?'Saved baseline':b.kind==='checkpoint'?'Full checkpoint':'Candidate validation'} · ${b.progress.evaluated||0}/${b.progress.total} responses evaluated · ${b.requires_revalidation?'Historical scores from a previous evaluator; revalidation required.':b.status==='complete'?`${b.report.total} final scenario outcomes; sealed experiment.`: 'Results are provisional until the experiment completes.'}`:'No benchmark recorded yet.'):`${report?.total||0} of ${s.monitor.window} conversations · Latest response per conversation within 24 hours${scope==='scenario'?' · Current demo campaign; real responses to synthetic inputs.':'.'}${s[scope+'_status']!=='connected'?' '+s[scope+'_status']:''}`;
 const n=$('#metrics');n.replaceChildren();
 Object.entries(names).forEach(([key,title])=>{const m=report?.metrics?.[key],warm=!offline&&(!m||m.n<s.monitor.minimum_samples),warn=!warm&&m?.pass_rate!=null&&m.pass_rate<s.monitor.threshold;
 const c=el('article',undefined,'card'),head=el('div',undefined,'card-title');head.append(el('span',title),badge(offline?(b?.requires_revalidation?'historical':b?.status||'pending'):warm?'warming up':warn?'below '+Math.round(s.monitor.threshold*100)+'%':'healthy',warn?'fail':warm?'pending':'pass'));c.append(head,el('div',pct(m?.pass_rate),'value'+(warn?' warn':'')));
 c.append(el('div',m?`${m.pass} pass / ${m.n} applicable`:'No evaluations yet','small'));const bar=el('div',undefined,'bar'+(warn?' warn':'')),fill=el('span');fill.style.width=((m?.pass_rate||0)*100)+'%';bar.append(fill);c.append(bar);
 c.append(el('div',`${m?.pending||0} pending · ${m?.unknown||0} unknown · ${m?.not_applicable||0} N/A`,'small'));
 if(warm)c.append(el('div',`Needs ${s.monitor.minimum_samples} applicable results to warn`,'small'));
 const bm=(base?.report||base?.current)?.metrics?.[key];if(b?.status==='complete'&&base?.status==='complete'&&bm&&m?.pass_rate!=null&&bm.pass_rate!=null){const delta=(m.pass_rate-bm.pass_rate)*100;c.append(el('div',`Baseline ${pct(bm.pass_rate)} → ${delta>=0?'+':''}${delta.toFixed(1)} pp`,'small'))}
 n.append(c)});
 $('#policy').textContent=`${s.monitor.window} conversations · Warn below ${Math.round(s.monitor.threshold*100)}%`;
 $('#method-policy').textContent=`All four metrics use the latest ${s.monitor.window} distinct conversations within 24 hours, separated by agent, evaluator and traffic source. Each needs ${s.monitor.minimum_samples} applicable results. Below ${pct(s.monitor.threshold)} triggers a warning and validated fix proposal. Recovery is ${pct(s.monitor.recovery_threshold)}. Demo scenarios start a fresh window for each campaign.`;
}
function renderActivity(){
 const s=snapshot,c=s.scenario_campaign;
 const incident=c?s.incidents.find(i=>i.created>=c.created&&i.payload.source==='scenario'):null;
 const baseline=s.benchmarks.find(b=>b.id===incident?.payload.validation_baseline_id);
 const repair=incident?s.repairs.find(r=>r.incident_id===incident.id):null;
 const currentExperiment=s.benchmarks.find(b=>b.id===(repair?.payload.targeted_benchmark_id||repair?.payload.candidate_benchmark_id))||baseline;
 const progress=c?.status==='awaiting_repair'&&currentExperiment?` ${currentExperiment.kind==='baseline'?'Original baseline':'Candidate experiment'}: ${currentExperiment.progress.total} responses recorded, ${currentExperiment.progress.evaluated||0} evaluated (${currentExperiment.status}).`:'';
 $('#workflow').textContent=s.provider_block?s.provider_block.message:c?`Demo workflow: ${(labels[c.status]||c.status).replaceAll('_',' ')} · ${c.completed} conversations executed.${progress||' '+(c.note||'')}`:'Monitoring real conversations. A qualifying warning starts baseline measurement and a reviewed fix.';
 const filter=$('#activity-filter').value,all=[...s.incidents.map(x=>({...x,kind:'incident'})),...s.repairs.map(x=>({...x,kind:'repair'}))].filter(x=>filter==='all'||x.kind===filter).sort((a,b)=>b.created-a.created);
 const n=$('#activity');n.replaceChildren();if(!all.length)empty(n,'No escalations or proposed fixes yet.');all.slice(0,activityLimit).forEach(x=>{const row=el('div',undefined,'activity-row'),desc=el('div');
 const title=x.kind==='repair'?(x.payload.pr_url?'PR #'+x.payload.pr_url.split('/').pop()+' · Agent improvement':'Agent improvement · '+x.id.slice(0,8)):x.payload.source==='checkpoint'?`${names[x.payload.metric]||x.payload.metric} · Checkpoint regression`:`${names[x.payload.metric]||x.payload.metric} below ${pct(x.payload.threshold)}`;
 desc.append(el('div',title,'title'),el('div',`${x.kind==='repair'?'Proposed fix':x.payload.source+' conversation window'}${x.requires_revalidation?' · Previous evaluator':''}`,'small'));
 row.append(badge(x.status),desc,el('time',dt(x.created),'small'),action('Details ↗',()=>showActivity(x)));n.append(row)});
 $('#more-activity').hidden=all.length<=activityLimit;
 $('#active-versions').textContent=`Running agent ${s.serving_agent.version.slice(0,12)} · Evaluator ${s.evaluator_version.slice(0,12)}. Candidate PRs do not change the running agent.`;
 const key=JSON.stringify(s.version_updates),v=$('#versions');if(v.dataset.key!==key){v.dataset.key=key;v.replaceChildren();s.version_updates.forEach(x=>{const d=el('details',undefined,'item');const summary=el('summary',`${x.kind} ${x.version?.slice(0,12)||'pending'} · ${dt(x.at)} · `);summary.append(badge(x.status));d.append(summary,el('p',x.summary),el('p',x.detail,'small'));if(x.pr_url)d.append(link('Review PR ↗',x.pr_url));v.append(d)})}
}
function openDetail(title,review=false){selectedRun=null;$('#detail-title').textContent=title;$('#review-panel').hidden=!review;$('#review-panel').open=false;$('#review').reset();$('#review-status').textContent='';const n=$('#detail-body');n.replaceChildren();if(!$('#detail').open)$('#detail').showModal();return n}
function rawDetails(n,title,value){const d=el('details');d.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));n.append(d)}
function showActivity(x){
 const n=openDetail(x.kind==='repair'?'Proposed fix':'Escalation'),p=x.payload;n.append(badge(x.status),el('p',dt(x.created),'small'),el('p',p.summary||p.description||''));if(x.requires_revalidation)n.append(el('p','Historical evidence from a previous evaluator. It does not establish current performance.','error'));
 if(p.pr_url)n.append(link(x.status==='pr_closed'?'View closed PR ↗':'Review draft PR ↗',p.pr_url));if(p.closed_reason)n.append(el('p',p.closed_reason));
 if(p.measurement)n.append(el('p',`${names[p.metric]}: ${pct(p.measurement.pass_rate)} (${p.measurement.pass}/${p.measurement.n} applicable), warning threshold ${pct(p.threshold)}.`));
 const incidentId=x.kind==='incident'?x.id:x.incident_id,incident=snapshot.incidents.find(i=>i.id===incidentId);
 const ids=(incident?.payload||p).failing_run_ids||[];if(ids.length){n.append(el('h3','Triggering evidence'));const box=el('div');ids.forEach((id,i)=>box.append(action('Failed response '+(i+1),()=>inspect(id))));n.append(box)}
 ['baseline_id','validation_baseline_id','candidate_benchmark_id','targeted_benchmark_id','benchmark_id'].forEach(key=>{const b=snapshot.benchmarks.find(b=>b.id===p[key]);if(b){n.append(el('h3',key==='targeted_benchmark_id'?'Targeted development check':key==='candidate_benchmark_id'||key==='benchmark_id'?'Candidate / checkpoint':'Before: baseline'),link('Open measured Phoenix experiment ↗',b.experiment_url));if(b.report)rawDetails(n,'Experiment results',b.report)}});
 const emails=snapshot.emails.filter(e=>e.incident_id===incidentId);n.append(el('h3','Email delivery'));if(!emails.length)n.append(el('p','No received email is recorded for this escalation.','muted'));
 emails.forEach(e=>{const d=el('details');d.append(el('summary',`${e.accepted?'SMTP accepted · '+e.smtp_code:'Received locally'} · ${dt(e.received)}`),el('p','Local demo inbox; no external delivery is claimed.','small'),el('p',`To: ${e.recipient} · Subject: ${e.subject}`),el('p','Message-ID: '+e.id,'small version-id'),el('p',e.smtp_response||'','small'),el('pre',e.body));n.append(d)});rawDetails(n,'Recorded workflow evidence',p);
}
async function inspect(id){
 const n=openDetail('Execution details',true);selectedRun=id;n.append(el('p','Loading execution and Phoenix trace…'));
 try{const data=await get('/quality/runs/'+encodeURIComponent(id));if(selectedRun!==id)return;n.replaceChildren(el('p','Trace '+data.event.trace_id,'small version-id'),el('p',`${dt(data.created)} · Agent ${data.event.version.slice(0,12)} · ${data.source}`,'small'));
 n.append(el('h3','Final answer'),el('pre',data.event.output));const scores=el('div');Object.entries(names).forEach(([key,title])=>{const m=data.evaluation?.metrics?.[key],d=el('details'),summary=el('summary',title+' · ');summary.append(badge(m?.label||'pending'));d.append(summary,el('p',m?.explanation||'Evaluation pending.'));scores.append(d)});n.append(el('h3','Evaluations'),scores);if(data.evaluation?.version!==snapshot.evaluator_version)n.append(el('p','Evaluation is pending or uses a previous evaluator.','small'));
 renderTrace(data.trace,n);rawDetails(n,'Conversation input',data.event.input);rawDetails(n,'Tool diagnostics & evaluation evidence',data.evaluation);rawDetails(n,'Previous evaluations (preserved)',data.evaluation_history||[]);
 }catch(e){if(selectedRun===id)n.replaceChildren(el('p',e.message,'error'))}
}
async function loadHistory(){
 const seq=++historySequence,q=new URLSearchParams({source:$('#trace-source').value,limit:'12'});if(historyCursor)q.set('cursor',historyCursor);
 try{const h=await get('/quality/runs?'+q);if(seq!==historySequence)return;nextCursor=h.next_cursor;const n=$('#traces');n.replaceChildren();h.items.forEach(x=>{const row=el('tr'),q=el('td');q.append(el('div',x.question,'question'),el('div',x.trace_id,'trace-id'));const scores=Object.values(x.metrics||{}),fail=scores.filter(m=>m.label==='fail').length,unknown=scores.some(m=>m.label==='unknown');const quality=x.requires_revalidation?'old evaluator':scores.length<4?'pending':fail?fail+' failed':unknown?'unknown':'no failures';const qualityCell=el('td');qualityCell.append(badge(quality,fail?'fail':quality==='no failures'?'pass':'pending'));const detail=el('td');detail.append(action('Inspect ↗',()=>inspect(x.id)));row.append(q,el('td',x.source==='scenario'?'Demo scenario':x.source),qualityCell,el('td',String(x.tool_count)),el('td',dt(x.created),'small'),detail);n.append(row)});if(!h.items.length){const row=el('tr'),cell=el('td','No recorded responses for this source.','empty');cell.colSpan=6;row.append(cell);n.append(row)}$('#trace-count').textContent=`${h.total} recorded responses · Page ${historyStack.length+1}`;$('#older').disabled=!nextCursor;$('#newer').disabled=!historyStack.length;
 }catch(e){$('#trace-count').textContent=e.message}
}
async function refresh(){if(refreshing)return;refreshing=true;try{snapshot=await get('/quality/state');benchmarkOptions();renderMetrics();renderActivity();$('#connection').textContent='Updated '+new Date().toLocaleTimeString();$('#connection').className='';if(!historyCursor)await loadHistory()}catch(e){$('#connection').textContent=e.message;$('#connection').className='error'}finally{refreshing=false}}
document.querySelectorAll('[data-scope]').forEach(n=>n.onclick=()=>{scope=n.dataset.scope;if(snapshot)renderMetrics()});$('#benchmark').onchange=renderMetrics;$('#activity-filter').onchange=()=>{activityLimit=6;renderActivity()};$('#more-activity').onclick=()=>{activityLimit+=10;renderActivity()};$('#trace-source').onchange=()=>{historyCursor=null;historyStack=[];loadHistory()};$('#older').onclick=()=>{historyStack.push(historyCursor);historyCursor=nextCursor;loadHistory()};$('#newer').onclick=()=>{historyCursor=historyStack.pop()||null;loadHistory()};$('#close').onclick=()=>{$('#detail').close();selectedRun=null};$('#detail').addEventListener('close',()=>selectedRun=null);
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
