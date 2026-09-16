function renderTrace(trace,node){
 node.append(el('h3','Trace call tree'),el('p',trace?.message||'Trace details unavailable.','small muted'),el('p','Execution OK means the call completed. Answer quality and tool-contract failures are shown under Evaluations and tool diagnostics.','small muted'));
 if(!trace?.spans?.length)return;
 const counts=trace.spans.reduce((a,s)=>(a[s.kind]=(a[s.kind]||0)+1,a),{});
 node.append(el('p',Object.entries(counts).map(([k,n])=>n+' '+k.toLowerCase()+' call'+(n===1?'':'s')).join(' · '),'small'));
 trace.spans.forEach(s=>{const call=el('details',undefined,'trace-call');call.style.marginLeft=Math.min(s.depth,5)*15+'px';const title=el('summary');
 title.append(document.createTextNode(s.kind+' · '+s.name+' · '+s.duration_ms.toLocaleString()+' ms · '),badge(s.status));call.append(title);
 call.append(el('div','Span '+s.id+' · Parent '+(s.parent_id||'root'),'small version-id'));
 if(s.model)call.append(el('div','Model: '+s.model,'small'));
 if(s.tokens.total!=null||s.tokens.prompt!=null||s.tokens.completion!=null)call.append(el('div','Tokens: '+(s.tokens.prompt??'—')+' input · '+(s.tokens.completion??'—')+' output · '+(s.tokens.total??'—')+' total','small'));
 if(s.error_type)call.append(el('div','Error: '+s.error_type,'error'));
 call.append(el('h3',s.kind==='TOOL'?'Tool arguments':'Input'),el('pre',JSON.stringify(s.input,null,2)??'Not recorded'),el('h3',s.kind==='TOOL'?'Tool result':'Output'),el('pre',JSON.stringify(s.output,null,2)??'Not recorded'));
 const attrs=el('details');attrs.append(el('summary','All recorded attributes'),el('pre',JSON.stringify(s.attributes,null,2)));call.append(attrs);node.append(call)});
 if(trace.truncated)node.append(el('p','Showing the first 200 returned spans. Open Phoenix for the full trace.','small'));
}
