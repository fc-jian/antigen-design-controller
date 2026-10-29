const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
test('native resource labels and process state do not claim Slurm acceptance', async () => {
  const h=harness();
  h.run('globalResources={settings:{gpu_targets:["gpu-site"],msa_backends:["primary"],queue_grace_seconds:120},gpu_catalog:[{id:"gpu-site",host:"gpu-site",backend:"baremetal",design:{},resources:{},label:"gpu-site · 8 × S5000 · 裸金属"}],msa_catalog:[]};renderResourceSettings()');
  assert.match(h.element('resource-gpu-options').innerHTML,/S5000/);
  assert.doesNotMatch(h.element('resource-gpu-options').innerHTML,/undefined/);
  h.context.crypto={randomUUID:()=> 'a'.repeat(32)};
  h.element('preview-dialog').showModal=()=>{};
  h.run('request=()=>({name:"native",profile:"auto"}); api=async()=>({expected_predictions:1,stages:["fold"],warnings:[],gpu_routing:{enabled:true,poll_seconds:45,queue_grace_seconds:120,targets:globalResources.gpu_catalog}})');
  await h.run('preview("fold")');
  assert.match(h.element('preview-body').innerHTML,/S5000/);
  assert.doesNotMatch(h.element('preview-body').innerHTML,/gpu-site \/ undefined/);
  h.context.requestAPI=async()=>({...job('a'),tasks:[{id:'fold',stage:'fold',host:'gpu-site',backend:'baremetal',state:'running',execution_state:'RUNNING',slurm_state:null,job_id:'native-test',attempts:1,observation:{backend:'baremetal',state:'RUNNING',pid:1234}}]});
  h.run('api=requestAPI');
  await h.run('openJob()');
  assert.match(h.element('job-detail').innerHTML,/裸金属 · RUNNING · PID 1234/);
  assert.doesNotMatch(h.element('job-detail').innerHTML,/<th>Slurm<\/th>/);
});

test('completed native duration uses start/end across refreshes and a new page', async () => {
  const h=harness();
  let value={...job('a'),state:'succeeded',tasks:[{id:'fold',stage:'fold',host:'gpu-site',backend:'baremetal',state:'succeeded',attempts:1,
    observation:{backend:'baremetal',state:'COMPLETED',elapsed:9999,started_at:1793102695.0558474,finished_at:1793102796.1560564}}]};
  h.context.requestAPI=async()=>value;h.run('api=requestAPI');await h.run('openJob()');
  assert.match(h.element('job-detail').innerHTML,/00:01:41\.10/);
  assert.match(h.element('job-detail').innerHTML,/开始 .*<br>结束 /);
  value={...value,observed_at:'later',tasks:[{...value.tasks[0],observation:{...value.tasks[0].observation,elapsed:99999}}]};
  await h.run('openJob()');
  assert.match(h.element('job-detail').innerHTML,/00:01:41\.10/);
  assert.doesNotMatch(h.element('job-detail').innerHTML,/99999/);
  const reloaded=harness();reloaded.context.requestAPI=async()=>value;reloaded.run('api=requestAPI');await reloaded.run('openJob()');
  assert.match(reloaded.element('job-detail').innerHTML,/00:01:41\.10/);
});

test('unknown terminal native end does not display an old increasing duration',()=>{
  const h=harness();
  for(const state of ['COMPLETED','FAILED','CANCELLED','TIMEOUT']){
    h.context.task={backend:'baremetal',observation:{state,elapsed:9999,started_at:100}};
    assert.equal(h.run('taskElapsed(task)'),'—');
  }
  assert.equal(h.run('taskElapsed({observation:{elapsed:"00:35:52"}})'),'00:35:52');
});

test('numeric elapsed has stable centisecond formatting, including zero and rounding',()=>{
  const h=harness();
  assert.equal(h.run('formatElapsed(0)'),'00:00:00.00');
  assert.equal(h.run('formatElapsed(59.999)'),'00:01:00.00');
  assert.equal(h.run('formatElapsed(360000)'),'100:00:00.00');
  for(const value of ['null','NaN','Infinity','-1'])assert.equal(h.run(`formatElapsed(${value})`),'—');
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
function harness() {
  const elements = new Map(), models = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', innerHTML: '', textContent: '', hidden: false,
      addEventListener() {}, querySelectorAll() {return [];},
      querySelector() {return {append() {}};}, scrollIntoView() {},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => [], createElement: () => ({dataset: {},addEventListener() {}})},
    window: {addEventListener() {},location:{hash:''},history:{pushState() {}}}, URLSearchParams, AbortController, setTimeout, clearTimeout,
    $3Dmol: {createViewer() {return {addModel(text) {models.push(text);}, setStyle() {}, zoomTo() {}, render() {}};}},
    fetch: async () => {throw new Error('Unexpected network access');},
  });
  const source = fs.readFileSync(path.join(__dirname, '../src/antigen_design/web/app.js'), 'utf8');
  vm.runInContext(source.replace(/start\(\)\.catch\(error=>notify\(error.message,true\)\);\s*$/, ''), context);
  const run = code => vm.runInContext(code, context);
  run('currentPage="project";currentJob="a";');
  return {context, run, element, models};
}
function job(id) {
  return {id, name: id, mode: 'folding', profile: 'test', state: 'running', tasks: [], results: []};
}

test('creation names start empty and whitespace is rejected before preview', () => {
  const html=fs.readFileSync(path.join(__dirname,'../src/antigen_design/web/index.html'),'utf8');
  for(const prefix of ['fold','design']) {
    const input=html.match(new RegExp(`<input id="${prefix}-name"[^>]*>`))[0];
    assert.doesNotMatch(input,/\bvalue=/);
    const h=harness();h.element(prefix+'-name').value='   ';
    assert.throws(()=>h.run(`request('${prefix}')`),/请填写任务名称/);
  }
});

test('name-only changes refresh project heading with escaped text', async () => {
  const h=harness();let value=job('a');
  h.context.requestAPI=async()=>value;h.run('api=requestAPI');
  await h.run('openJob()');
  value={...value,name:'新项目 <example>'};
  await h.run('openJob()');
  assert.match(h.element('job-detail').innerHTML,/新项目 &lt;example&gt;/);
});
function result(id) {
  return {id, confidence: {ranking_score: id + 1}, metrics: {}, residues: [], pae: null};
}

test('late job response cannot overwrite the selected job', async () => {
  const h = harness(), a = deferred(), b = deferred();
  h.context.requestAPI = key => key === 'jobs/a/overview' ? a.promise : b.promise;
  h.run('api=requestAPI');
  const old = h.run('openJob()');
  const latest = h.run('currentJob="b";openJob()');
  b.resolve(job('b')); await latest;
  const displayed = h.element('job-detail').innerHTML;
  a.resolve(job('a')); await old;
  assert.equal(h.element('job-detail').innerHTML, displayed);
  assert.match(displayed, /<h2>b /);
});

test('late result JSON and structure responses cannot mix samples', async () => {
  const h = harness(), firstJSON = deferred(), firstStructure = deferred();
  h.run('currentPage="result"');
  const j = {...job('a'), results: [{id: 0}, {id: 1}]};
  h.context.selectedJob = j;
  h.context.requestAPI = key => key.endsWith('/0') ? firstJSON.promise : Promise.resolve(result(1));
  h.context.fetch = url => url.endsWith('/0/structure') ? firstStructure.promise : Promise.resolve({ok: true, text: async () => 'structure-1'});
  h.run('api=requestAPI');
  const old = h.run('showResult(selectedJob)');
  firstJSON.resolve(result(0));
  await Promise.resolve(); await Promise.resolve();
  const latest = h.run('currentResult=1;showResult(selectedJob)');
  await latest;
  firstStructure.resolve({ok: true, text: async () => 'structure-0'});
  await old;
  assert.deepEqual(h.models, ['structure-1']);
  assert.match(h.element('result-content').innerHTML, /results\/1\/structure/);

  const lateJSON = deferred();
  h.context.requestAPI = key => key.endsWith('/0') ? lateJSON.promise : Promise.resolve(result(1));
  const obsolete = h.run('currentResult=0;showResult(selectedJob)');
  await h.run('currentResult=1;showResult(selectedJob)');
  lateJSON.resolve(result(0)); await obsolete;
  assert.deepEqual(h.models, ['structure-1', 'structure-1']);
});

test('switching jobs invalidates an in-flight structure response', async () => {
  const h = harness(), body = deferred();
  h.run('currentPage="result"');
  h.context.selectedJob = {...job('a'), results: [{id: 0}]};
  h.context.requestAPI = async () => result(0);
  h.context.fetch = async () => ({ok: true, text: () => body.promise});
  h.run('api=requestAPI');
  const pending = h.run('showResult(selectedJob)');
  await Promise.resolve(); await Promise.resolve();
  h.run('currentJob="b"');
  body.resolve('old structure'); await pending;
  assert.deepEqual(h.models, []);
});

test('monitor errors redraw even when observation timestamps are unchanged', async () => {
  const h = harness();
  let value = job('a');
  h.context.requestAPI = async () => value;
  h.run('api=requestAPI');
  await h.run('openJob()');
  value = {...value, monitor_error: 'connection failed'};
  await h.run('openJob()');
  assert.match(h.element('job-detail').innerHTML, /connection failed/);
});

test('structure HTTP errors are not rendered as models', async () => {
  const h = harness();
  h.run('currentPage="result"');
  h.context.selectedJob = {...job('a'), results: [{id: 0}]};
  h.context.requestAPI = async () => result(0);
  h.context.fetch = async () => ({ok: false, status: 404});
  h.run('api=requestAPI');
  await assert.rejects(h.run('showResult(selectedJob)'), /HTTP 404/);
  assert.deepEqual(h.models, []);
});

test('new filters win over older list responses', async () => {
  const h = harness(), old = deferred();
  h.element('filter-archive').value = 'current';
  h.context.requestAPI = key => {
    if (key === 'health') return Promise.resolve({enabled: true, alive: true, errors: {}});
    if (key.includes('archive=current')) return old.promise;
    return Promise.resolve({items: [], total: 0, counts: {total: 30, active: 0, succeeded: 30, problem: 0}});
  };
  h.run('api=requestAPI;currentJob=null');
  const stale = h.run('refreshJobs()');
  h.element('filter-archive').value = 'archived';
  await h.run('refreshJobs(true)');
  old.resolve({items: [], total: 0, counts: {total: 1}}); await stale;
  assert.equal(h.element('job-count').textContent, 30);
});

test('all confidence and design sequence colors have readable text', () => {
  const h = harness();
  const colors = h.run('[null,0,50,70,90,100].map(confidenceColor).concat([true,false,null].map(designColor))');
  const luminance = color => {
    const rgb = color.slice(1).match(/../g).map(v => parseInt(v,16)/255).map(v => v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4);
    return .2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2];
  };
  for (const background of colors) {
    const foreground = h.run(`readableText(${JSON.stringify(background)})`);
    const a = luminance(background), b = luminance(foreground);
    assert.ok((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5, `${background} ${foreground}`);
  }
  assert.notEqual(h.run('designColor(true)'),h.run('designColor(false)'));
});


test('completed scheduler MSA still shows pending return and escaped error', async () => {
  const h=harness();
  h.context.requestAPI=async()=>({...job('a'),tasks:[{id:'msa',stage:'msa',host:'msa-server',state:'running',slurm_state:'COMPLETED',attempts:1,transfer_phase:'returning',transfer_error:'<untrusted>'}]});
  h.run('api=requestAPI');await h.run('openJob()');
  const html=h.element('job-detail').innerHTML;
  assert.match(html,/回传结果/);assert.match(html,/COMPLETED/);assert.match(html,/&lt;untrusted&gt;/);
});


test('submission preview identifies global MSA allocation', async () => {
  const h=harness();h.element('preview-dialog').showModal=()=>{};
  h.context.crypto={randomUUID:()=> 'a-b-c'};
  h.context.requestAPI=async()=>({expected_predictions:1,cpu_host:'cluster-cpu',msa_host:'msa-server',gpu_host:'cluster-cpu',stages:['msa','fold'],warnings:[]});
  h.run('api=requestAPI;request=()=>({name:"test",profile:"remote-msa"})');
  await h.run('preview("fold")');
  assert.match(h.element('preview-body').innerHTML,/全局自动分配/);
  assert.match(h.element('preview-body').innerHTML,/msa-server/);
});


test('pool request completion is distinct from running allocation', async () => {
  const h=harness();
  h.context.requestAPI=async()=>({...job('a'),tasks:[{id:'msa',stage:'msa',host:'msa-server',state:'succeeded',slurm_state:'RUNNING',attempts:1,pool_state:'succeeded',pool_phase:'ready',pool:{request_id:'request'}}]});
  h.run('api=requestAPI');await h.run('openJob()');
  const html=h.element('job-detail').innerHTML;
  assert.match(html,/共享分配/);assert.match(html,/RUNNING/);
});

test('preview retains shared index pool details in resource evidence', async () => {
  const h=harness();h.element('preview-dialog').showModal=()=>{};
  h.context.crypto={randomUUID:()=> 'a-b-c'};
  h.context.requestAPI=async()=>({expected_predictions:1,cpu_host:'cluster-cpu',msa_host:'msa-server',gpu_host:'cluster-cpu',msa_pool:{enabled:true,idle_seconds:300},stages:['msa','fold'],warnings:[]});
  h.run('api=requestAPI;request=()=>({name:"test",profile:"remote-msa-pool"})');
  await h.run('preview("fold")');
  assert.match(h.element('preview-body').innerHTML,/msa_pool/);
});


test('MSA preview describes confirmed cancellation before resource rerouting', async()=>{
 const h=harness();h.element('preview-dialog').showModal=()=>{};h.context.crypto={randomUUID:()=> 'priority'};
 h.context.requestAPI=async()=>({expected_predictions:1,cpu_host:'cluster-cpu',msa_host:'msa-server',msa_fallback_host:'cluster-cpu',msa_fallback_when_busy:true,gpu_host:'cluster-cpu',stages:['msa','fold'],warnings:[]});
 h.run('api=requestAPI;request=()=>({name:"priority",profile:"cluster-cpu"})');await h.run('preview("fold")');
 assert.match(h.element('preview-body').innerHTML,/全局自动调度/);assert.match(h.element('preview-body').innerHTML,/确认取消后转投/);
});

test('project creation sends no cluster scope',()=>{
 const h=harness();h.element('fold-name').value='auto';h.element('fold-seeds').value='101';h.element('fold-samples').value='1';h.element('fold-input').value='>ubq\nMQIFVKTL';
 const value=h.run('request("fold")');assert.equal(value.profile,undefined);assert.equal(value.mode,'folding');
 const html=fs.readFileSync(path.join(__dirname,'../src/antigen_design/web/index.html'),'utf8');
 assert.doesNotMatch(html,/id="(?:fold|design)-profile"/);assert.match(html,/id="save-resources"/);
});

test('project details are separate and scheduling follows the table collapsed', async()=>{
  const h=harness();
  h.context.requestAPI=async()=>({...job('a'),tasks:[{id:'fold_0',stage:'fold',host:'cpu',state:'succeeded',attempts:1,routing_reason:'ready'}],results:[{id:'metrics_0~0',name:'one',seed:101,sample:0}]});
  h.run('api=requestAPI');await h.run('openJob()');
  const html=h.element('job-detail').innerHTML;
  assert.ok(html.indexOf('</table>')<html.indexOf('id="task-scheduling"'));
  assert.match(html,/<details class="help" id="task-scheduling">/);
  assert.match(html,/#\/jobs\/a\/results\/metrics_0~0/);
  assert.doesNotMatch(html,/下载完整结果/);
  assert.equal(h.models.length,0);
  assert.equal(h.element('task-scheduling').open,false);
  const document=fs.readFileSync(path.join(__dirname,'../src/antigen_design/web/index.html'),'utf8');
  assert.ok(document.indexOf('id="page-project"')<document.indexOf('id="job-detail"'));
  assert.ok(document.indexOf('id="page-result"')<document.indexOf('id="results-panel"'));
});

test('polling preserves expanded scheduling but result view rejects late project responses', async()=>{
  const h=harness();let value=job('a');
  h.context.requestAPI=async()=>value;h.run('api=requestAPI');await h.run('openJob()');
  h.element('task-scheduling').open=true;value={...value,updated_at:'new'};
  await h.run('openJob()');assert.equal(h.element('task-scheduling').open,true);
  const late=deferred();h.context.requestAPI=()=>late.promise;
  const pending=h.run('openJob()');const old=h.element('job-detail').innerHTML;
  h.run('navigate("result",false)');late.resolve({...value,name:'obsolete'});await pending;
  assert.equal(h.element('job-detail').innerHTML,old);
});

test('deep links open a project directly without needing the current list page', async()=>{
  const h=harness(),id='a'.repeat(32);
  h.context.window.location.hash=`#/jobs/${id}`;
  h.context.requestAPI=async()=>job(id);h.run('api=requestAPI');await h.run('loadRoute()');
  assert.equal(h.run('currentPage'),'project');assert.equal(h.run('currentJob'),id);
  assert.match(h.element('job-detail').innerHTML,new RegExp(id));
});

test('large project renders one page and does not request collapsed details',async()=>{
  const h=harness(),calls=[];
  const value={...job('a'),tasks:Array.from({length:120},(_,i)=>({id:'task_'+i,stage:'fold',host:'cpu',state:'succeeded',attempts:1})),
    results:Array.from({length:120},(_,i)=>({id:'metrics_'+i+'~0',name:'result_'+i,seed:101,sample:0}))};
  h.context.requestAPI=async key=>{calls.push(key);return value;};h.run('api=requestAPI');
  await h.run('openJob()');
  let html=h.element('job-detail').innerHTML;
  assert.match(html,/task_49</);assert.doesNotMatch(html,/task_50</);
  assert.match(html,/result_49</);assert.doesNotMatch(html,/result_50</);
  assert.deepEqual(calls,['jobs/a/overview']);
  h.run('taskOffset=50;resultOffset=100;paintJob(displayedJob)');
  html=h.element('job-detail').innerHTML;
  assert.match(html,/task_50</);assert.doesNotMatch(html,/task_49</);
  assert.match(html,/result_119</);assert.doesNotMatch(html,/result_99</);
  assert.equal(calls.length,1); // Paging uses the compact snapshot already loaded.
});

test('list archive stays in the list and returns to the last nonempty page',async()=>{
  const h=harness(),calls=[];
  h.context.button={disabled:false,dataset:{archive:'a',archived:'false'}};
  h.context.requestAPI=async(key,data)=>{
    calls.push([key,data]);
    if(key==='jobs/a/archive')return {id:'a',archived:true};
    return {items:[],total:25,counts:{total:26}};
  };
  h.run('api=requestAPI;currentPage="monitor";jobsOffset=25');
  await h.run('archiveFromList(button)');
  assert.equal(calls[0][0],'jobs/a/archive');
  assert.equal(calls.length,3);
  assert.match(calls[1][0],/offset=25/);assert.match(calls[2][0],/offset=0/);
  assert.equal(h.run('currentPage'),'monitor');assert.equal(h.context.button.disabled,false);
});

test('archive buttons are available for stopped rows and restoration, never active rows',()=>{
  const h=harness();
  h.context.values=[{...job('active')},{...job('done'),state:'succeeded'},
    {...job('archived'),state:'failed',archived:true},{...job('broken'),state:'failed',unavailable:true}];
  h.run('jobs=values;renderJobs()');
  const html=h.element('jobs-body').innerHTML;
  assert.match(html,/data-archive="done"/);assert.match(html,/data-archive="archived"[^>]*>取消归档/);
  assert.doesNotMatch(html,/data-archive="(?:active|broken)"/);
});

test('new list request aborts obsolete fetch and does not wait for health',async()=>{
  const h=harness(),late=deferred(),signals=[];
  h.context.requestAPI=(key,data,options)=>{
    assert.notEqual(key,'health');signals.push(options.signal);
    return signals.length===1?late.promise:Promise.resolve({items:[],total:0,counts:{total:7}});
  };
  h.run('api=requestAPI;currentPage="monitor"');
  const old=h.run('refreshJobs()');await h.run('refreshJobs(true)');
  assert.equal(signals[0].aborted,true);
  late.resolve({items:[],total:0,counts:{total:1}});await old;
  assert.equal(h.element('job-count').textContent,7);
});
