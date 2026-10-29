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
  const elements = new Map(), models = [], viewers = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', innerHTML: '', textContent: '', hidden: false, listeners: {},
      addEventListener(type,listener) {this.listeners[type]=listener;}, querySelectorAll() {return [];},
      querySelector() {return {append() {}};}, scrollIntoView() {}, replaceChildren() {},
      focus() {this.focused=true;}, select() {this.selected=true;},
    });
    return elements.get(id);
  }
  const methods=[element('method-mpnn'),element('method-rfd3')];
  methods[0].value='mpnn';methods[0].checked=true;methods[1].value='rfd3';methods[1].checked=false;
  const context = vm.createContext({
    document: {getElementById: element, querySelector: selector=>selector==='input[name="method"]:checked'?methods.find(input=>input.checked):null,
      querySelectorAll: selector=>selector==='input[name="method"]'?methods:[], createElement: () => ({dataset: {},addEventListener() {}})},
    window: {addEventListener() {},location:{hash:''},history:{pushState() {}}}, URLSearchParams, AbortController, setTimeout, clearTimeout,
    $3Dmol: {createViewer() {
      const viewer={view:[viewers.length,2,3,-50,0,0,0,1],styles:[],zoomCalls:[],renderCount:0,
        addModel(text) {models.push(text);},setStyle(...args) {this.styles.push(args);},
        zoomTo(...args) {this.zoomCalls.push(args);},render() {this.renderCount++;},
        getView() {return this.view;},setView(view) {this.view=Array.from(view);},resize() {},setClickable() {},setHoverable() {}};
      viewers.push(viewer);return viewer;
    }},
    fetch: async () => {throw new Error('Unexpected network access');},
  });
  const source = fs.readFileSync(path.join(__dirname, '../src/antigen_design/web/app.js'), 'utf8');
  vm.runInContext(source.replace(/start\(\)\.catch\(error=>notify\(error.message,true\)\);\s*$/, ''), context);
  const run = code => vm.runInContext(code, context);
  run('currentPage="project";currentJob="a";');
  return {context, run, element, models, viewers};
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
    assert.throws(()=>h.run(`request('${prefix}')`),/请填写项目名称/);
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
function resultHarness(values) {
  const h=harness(),writes=[];
  h.context.selectedJob={...job('a'),name:'测试项目',results:values};
  h.context.requestAPI=async key=>values.find(value=>key.endsWith('/'+value.id));
  h.context.fetch=async url=>({ok:true,text:async()=>url});
  h.context.navigator={clipboard:{writeText:async text=>{writes.push(text);}}};
  h.run('api=requestAPI;currentPage="result"');
  return {...h,writes};
}

test('CIF filename identifies the project, backbone, sequence, seed and sample',()=>{
  const h=harness();
  h.context.fixtureResult={...result(0),metrics:{design:'b0012_s0034',fold_seed:7,sample:0}};
  assert.equal(h.run('structureDownloadName({name:"项目 / 名:称",id:"a"},fixtureResult)'),
    '项目 _ 名_称-b012-s0034-seed007-sample000.cif');
  assert.equal(h.run('structureDownloadName({name:"改名后"},fixtureResult)'),
    '改名后-b012-s0034-seed007-sample000.cif');
  h.context.fixtureResult={...result('metrics_input_0009_seed101~0'),seed:101,sample:0};
  assert.equal(h.run('structureDownloadName({name:"Folding"},fixtureResult)'),
    'Folding-b000-s0009-seed101-sample000.cif');
  h.context.fixtureResult={...result('metrics_b1000_s10000_seed10000~1000'),seed:10000,sample:1000};
  assert.equal(h.run('structureDownloadName({name:"large"},fixtureResult)'),
    'large-b1000-s10000-seed10000-sample1000.cif');
  assert.match(h.run('structureDownloadName({name:"legacy"},{id:0})'),/-bunknown-sunknown-seedunknown-sampleunknown\.cif$/);
});

test('FASTA preserves chain boundaries and unknown residues while omitting ions and water',()=>{
  const h=harness();
  h.context.fixtureResult={...result(0),name:'fixture\nexample',seed:101,sample:0,residues:[
    {chain:'B',resname:'ALA'},{chain:'A',resname:'GLY'},
    {chain:'B',resname:'MSE'},{chain:'A',resname:'UNK'},
    {chain:'RNA',resname:'A',atoms:["C1'","C3'","C4'"]},{chain:'RNA',resname:'U'},{chain:'RNA',resname:'modified'},
    {chain:'ion',resname:'CA',atoms:['CA']},{chain:'water',resname:'HOH',atoms:['O']},
    {chain:'modified',resname:'modified',atoms:['N','CA','C']},
  ]};
  assert.equal(h.run('resultFasta(fixtureResult)'),
    '>fixture_example|chain=B seed=101 sample=0\nAM\n>fixture_example|chain=A seed=101 sample=0\nGX\n>fixture_example|chain=RNA seed=101 sample=0\nAUN\n>fixture_example|chain=modified seed=101 sample=0\nX\n');
});

test('copy and CIF download follow the selected result, even while the project is running',async()=>{
  const values=[0,1].map(i=>({...result(`metrics_b0002_s000${i}_seed101~${i}`),
    name:'fixture',seed:101,sample:i,metrics:{design:`b0002_s000${i}`},residues:[{chain:'A',resname:i?'GLY':'ALA'}]}));
  const h=resultHarness(values);
  await h.run('showResult(selectedJob)');
  const staleCopy=h.element('copy-result-sequence').listeners.click;
  await staleCopy();
  assert.equal(h.writes[0],'>fixture|chain=A seed=101 sample=0\nA\n');
  assert.equal(h.element('copy-result-sequence').disabled,false);
  assert.match(h.element('result-action-status').textContent,/已复制/);
  await h.run('currentResult=1;showResult(selectedJob)');
  await staleCopy();assert.equal(h.writes.length,1);
  await h.element('copy-result-sequence').listeners.click();
  assert.equal(h.writes[1],'>fixture|chain=A seed=101 sample=1\nG\n');
  const html=h.element('result-content').innerHTML;
  assert.match(html,/href="\/api\/jobs\/a\/results\/metrics_b0002_s0001_seed101~1\/structure" download="测试项目-b002-s0001-seed101-sample001.cif"/);
});

test('clipboard failure exposes selectable FASTA without claiming successful copy',async()=>{
  for(const clipboard of [undefined,{writeText:async()=>{throw new Error('Denied');}}]) {
    const h=resultHarness([{...result(0),residues:[{chain:'A',resname:'ALA'}]}]);
    h.context.navigator={clipboard};
    await h.run('showResult(selectedJob)');
    await h.element('copy-result-sequence').listeners.click();
    assert.equal(h.element('result-sequence-fallback').hidden,false);
    assert.match(h.element('result-sequence-text').value,/>0\|chain=A\nA\n/);
    assert.equal(h.element('result-sequence-text').selected,true);
    assert.equal(h.element('result-sequence-text').focused,true);
    assert.equal(h.element('copy-result-sequence').disabled,false);
    assert.match(h.element('result-action-status').textContent,/手动复制/);
    h.context.navigator={clipboard:{writeText:async()=>{}}};
    await h.element('copy-result-sequence').listeners.click();
    assert.equal(h.element('result-sequence-fallback').hidden,true);
    assert.match(h.element('result-action-status').textContent,/已复制/);
  }
});

test('late clipboard errors do not expose an old sequence on the new result',async()=>{
  const h=resultHarness([0,1].map(i=>({...result(i),residues:[{chain:'A',resname:'ALA'}]}))),copy=deferred();
  h.context.navigator.clipboard.writeText=()=>copy.promise;
  await h.run('showResult(selectedJob)');
  const old=h.element('copy-result-sequence').listeners.click();
  await h.run('currentResult=1;showResult(selectedJob)');
  h.element('result-action-status').textContent='new result';
  copy.reject(new Error('Denied'));await old;
  assert.equal(h.element('result-action-status').textContent,'new result');
  assert.equal(h.element('result-sequence-fallback').hidden,true);
});

test('reset restores each current model camera and full clipping without repainting its colors',async()=>{
  const h=resultHarness([result(0),result(1)]);
  await h.run('showResult(selectedJob)');
  const first=h.viewers[0],initial=[...first.view],resetFirst=h.element('reset-result-view').listeners.click;
  first.view.fill(42); // Include pan, zoom and rotation; the saved view must be a copy.
  const paints=first.styles.length;
  resetFirst();
  assert.deepEqual(first.view,initial);
  assert.deepEqual(first.zoomCalls,[[],[]]);
  assert.equal(first.styles.length,paints);
  await h.run('currentResult=1;showResult(selectedJob)');
  const second=h.viewers[1],secondInitial=[...second.view];
  second.view.fill(99);
  resetFirst();assert.equal(second.view[0],99);
  h.element('reset-result-view').listeners.click();
  assert.deepEqual(second.view,secondInitial);
  assert.equal(h.element('reset-result-view').disabled,false);
});

test('copy and download remain available without WebGL, while empty sequences cannot be copied',async()=>{
  const h=resultHarness([{...result('metrics_input_0000_seed101~0'),seed:101,sample:0,residues:[{chain:'A',resname:'ALA'}]}]);
  h.context.$3Dmol.createViewer=()=>{throw new Error('No WebGL');};
  await h.run('showResult(selectedJob)');
  await h.element('copy-result-sequence').listeners.click();
  assert.equal(h.writes.length,1);
  assert.equal(h.run('resultViewer'),null);
  assert.match(h.element('result-content').innerHTML,/<button id="reset-result-view"[^>]*disabled/);
  assert.match(h.element('result-content').innerHTML,/download="测试项目-b000-s0000-seed101-sample000.cif"/);
  const empty=resultHarness([result(0)]);
  await empty.run('showResult(selectedJob)');
  assert.equal(empty.element('copy-result-sequence').disabled,true);
  await empty.element('copy-result-sequence').listeners.click();
  assert.equal(empty.writes.length,0);
});

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


test('submission preview keeps input-specific warnings and global MSA allocation', async () => {
  const h=harness();h.element('preview-dialog').showModal=()=>{};
  h.context.crypto={randomUUID:()=> 'a-b-c'};
  h.context.requestAPI=async()=>({expected_predictions:1,cpu_host:'cluster-cpu',msa_host:'msa-server',gpu_host:'cluster-cpu',stages:['msa','fold'],warnings:[
    'RNA/DNA do not receive an MSA from the configured protein ColabFold search.',
    'Custom CCD, templates and supplied MSA fields are rejected rather than ignored.',
    '当前输入需要检查 <A:10>'
  ]});
  h.run('api=requestAPI;request=()=>({name:"test",profile:"remote-msa"})');
  await h.run('preview("fold")');
  assert.match(h.element('preview-body').innerHTML,/全局自动分配/);
  assert.match(h.element('preview-body').innerHTML,/msa-server/);
  const body=h.element('preview-body').innerHTML.split('<details>')[0];
  assert.match(body,/当前输入需要检查 &lt;A:10&gt;/);
  assert.doesNotMatch(body,/RNA\/DNA do not|Custom CCD, templates/);
  assert.match(h.element('preview-body').innerHTML,/Custom CCD, templates/);
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

test('relocated history remains viewable without compute actions',async()=>{
  const h=harness();
  h.context.requestAPI=async()=>({...job('a'),history_only:true,state:'attention',results:[{id:0,name:'fixture',seed:101,sample:0}]});
  h.run('api=requestAPI');await h.run('openJob()');
  const html=h.element('job-detail').innerHTML;
  assert.match(html,/已迁移的历史记录/);
  assert.match(html,/查看结构与指标/);
  assert.doesNotMatch(html,/data-action="(?:retry|resume|cancel)"/);
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

function inputSource(mode='folding') {
  return {id:'a',name:'Renamed source',request:{id:'a',name:'Original source',mode,profile:'legacy-host',
    input:' {"name":"fixture", "modelSeeds":[0,7],\n "sequences":[{"proteinChain":{"sequence":"ACD","count":1}}]}\n',
    seeds:[0,101],samples:3}};
}

test('late deployment defaults preserve original inputs and editable copies',async()=>{
  for(const mode of ['folding','design'])for(const editing of [false,true]) {
    const h=harness(),source=inputSource(mode),prefix=mode==='folding'?'fold':'design';
    source.request={...source.request,rfd3:false,seed:0,regions:[]};
    h.context.requestAPI=async()=>source;h.run('api=requestAPI');
    await h.run(`openProjectInput("a",${editing})`);
    h.run('applyConfiguredDefaults({seeds:[23],samples:4,seed:12,backbones:7,sequences:8,temperature:.3,rfd3:true})');
    assert.equal(h.element(prefix+'-seeds').value,'0, 101');
    assert.equal(h.element(prefix+'-samples').value,'3');
    assert.equal(h.element(prefix+'-name').readOnly,!editing);
    if(mode==='design') {
      assert.equal(h.run('diffusion()'),false);
      assert.equal(h.element('design-seed').value,'0');
    }
    h.run(`navigate("${mode}")`);
    assert.equal(h.element(prefix+'-seeds').value,'23');
    assert.equal(h.element(prefix+'-samples').value,'4');
    if(mode==='design') {
      assert.equal(h.run('diffusion()'),true);
      assert.equal(h.element('design-seed').value,'12');
      assert.equal(h.element('design-backbones').value,'7');
    }
  }
});

test('original input uses the complete creation form read-only and does not preview or submit',async()=>{
  const h=harness(),source=inputSource(),calls=[];
  h.context.requestAPI=async key=>{calls.push(key);return source;};h.run('api=requestAPI');
  await h.run('openProjectInput("a")');
  assert.deepEqual(calls,['jobs/a/input']);
  assert.equal(h.run('currentPage'),'folding');
  assert.equal(h.element('fold-name').value,'Original source');
  assert.equal(h.element('fold-input').value,source.request.input);
  assert.equal(h.element('fold-seeds').value,'0, 101');
  assert.equal(h.element('fold-samples').value,'3');
  assert.equal(h.element('fold-input').readOnly,true);
  assert.equal(h.element('fold-upload').disabled,true);
  assert.equal(h.element('fold-preview').hidden,true);
  assert.match(h.element('fold-source').innerHTML,/修改并建立新项目/);
  assert.match(h.element('fold-source').innerHTML,/legacy-host/);
  assert.throws(()=>h.run('request("fold")'),/修改并建立新项目/);
});

test('editing a copy requires a new name and submits a fresh ID with source provenance',async()=>{
  const h=harness(),source=inputSource(),calls=[];
  h.context.crypto={randomUUID:()=> 'b'.repeat(32)};
  h.element('preview-dialog').showModal=()=>{};h.element('preview-dialog').close=()=>{};
  h.context.requestAPI=async(key,data)=>{
    calls.push({key,data});
    if(key==='jobs/a/input')return source;
    if(key==='preview')return {expected_predictions:1,stages:['fold'],warnings:[]};
    return job('b'.repeat(32));
  };h.run('api=requestAPI');
  await h.run('openProjectInput("a")');h.run('beginProjectCopy()');
  assert.equal(h.element('fold-name').value,'');
  assert.equal(h.element('fold-input').readOnly,false);
  assert.equal(h.element('fold-preview').hidden,false);
  assert.throws(()=>h.run('request("fold")'),/请填写项目名称/);
  h.element('fold-name').value='New project';h.element('fold-input').value='>new fixture\nACD';
  h.element('fold-seeds').value='7';h.element('fold-samples').value='1';
  await h.run('preview("fold")');
  const pending=h.run('pending');
  assert.equal(pending.id,'b'.repeat(32));assert.equal(pending.source_job,'a');
  assert.equal(pending.profile,undefined);assert.equal(pending.name,'New project');
  assert.equal(pending.input,'>new fixture\nACD');
  await h.element('submit-job').listeners.click({currentTarget:h.element('submit-job')});
  assert.equal(calls.filter(c=>c.key==='jobs').length,1);
  assert.equal(calls.find(c=>c.key==='jobs').data.id,'b'.repeat(32));
  assert.equal(h.run('currentJob'),'b'.repeat(32));
  assert.equal(source.request.name,'Original source');
  assert.match(source.request.input,/modelSeeds/);
  h.run('clearTimeout(toastTimer)');
});

test('leaving original-input or copy view restores the separate unsubmitted creation draft',async()=>{
  const h=harness(),source=inputSource();
  h.element('fold-name').value='My unsent draft';h.element('fold-input').value='>draft\nGGG';
  h.element('fold-seeds').value='9';h.element('fold-samples').value='2';
  h.context.requestAPI=async()=>source;h.run('api=requestAPI');
  await h.run('openProjectInput("a",true)');
  h.element('fold-name').value='Edited copy';
  h.run('navigate("folding")');
  assert.equal(h.element('fold-name').value,'My unsent draft');
  assert.equal(h.element('fold-input').value,'>draft\nGGG');
  assert.equal(h.element('fold-seeds').value,'9');
  assert.equal(h.element('fold-samples').value,'2');
  assert.equal(h.element('fold-input').readOnly,false);
  assert.equal(h.element('fold-source').hidden,true);
  assert.equal(h.run('request("fold").source_job'),undefined);
});

test('original design restores insertion codes, separate regions and variable lengths exactly',async()=>{
  const h=harness(),source=inputSource('design');
  source.request={...source.request,asset:'asset-a',rfd3:true,backbones:3,sequences:4,temperature:.2,seed:0,
    regions:[{region:'A:11A-15',length:[2,4]},{region:'B:-2--1',length:[2,2]}]};
  source.asset={id:'asset-a',filename:'original.pdb',warnings:[],residues:[
    ['A',11,'A'],['A',14,''],['A',15,''],['B',-2,''],['B',-1,'']
  ].map(([chain,number,insertion],index)=>({index,chain,number,insertion,normalized_chain:chain,position:index+1,original_id:`${chain}:${number}${insertion}`,amino_acid:'A'}))};
  source.download_url='/api/jobs/a/input/file';
  h.context.requestAPI=async()=>source;h.context.fetch=async()=>({ok:true,text:async()=> 'original PDB'});
  h.run('api=requestAPI');await h.run('openProjectInput("a")');
  assert.deepEqual(h.models,['original PDB']);
  assert.equal(h.run('diffusion()'),true);
  assert.equal(h.element('design-backbones').value,'3');
  assert.equal(h.element('design-sequences').value,'4');
  assert.equal(h.element('design-temp').value,'0.2');
  assert.equal(h.element('design-seed').value,'0');
  assert.equal(h.element('add-region').disabled,true);
  h.run('pickResidue(0)');assert.equal(h.run('selected.size'),5);
  assert.match(h.element('region-list').innerHTML,/value="4"/);
  h.run('beginProjectCopy()');h.element('design-name').value='New design';
  const data=JSON.parse(h.run('JSON.stringify(request("design"))'));
  assert.deepEqual(data.regions,source.request.regions);
  assert.equal(data.asset,'asset-a');assert.equal(data.rfd3,true);
  assert.equal(data.source_job,'a');assert.equal(data.seed,0);
  assert.equal(data.backbones,3);assert.equal(data.sequences,4);
  assert.deepEqual(data.seeds,[0,101]);assert.equal(data.samples,3);
  h.run('navigate("design")');assert.equal(h.run('asset'),null);
  assert.equal(h.run('selected.size'),0);assert.equal(h.run('diffusion()'),false);
  assert.equal(h.element('design-name').value,'');
});

test('late original-input responses and old upload previews cannot replace the current page',async()=>{
  const h=harness(),late=deferred(),upload=deferred();
  h.context.requestAPI=()=>late.promise;h.context.fetch=()=>upload.promise;
  h.context.uploadValue={id:'old'};h.run('api=requestAPI');
  const oldUpload=h.run('loadStructure(uploadValue)');
  const oldInput=h.run('openProjectInput("a")');
  h.run('navigate("folding")');h.element('fold-input').value='keep this draft';
  late.resolve(inputSource());await oldInput;
  upload.resolve({ok:true,text:async()=> 'obsolete'});await oldUpload;
  assert.equal(h.element('fold-input').value,'keep this draft');
  assert.equal(h.run('projectInput'),undefined);assert.deepEqual(h.models,[]);
});

test('missing source structure is explicit and does not reuse a previous asset',async()=>{
  const h=harness(),source=inputSource('design');
  source.asset_error='原始结构校验失败';source.request.asset='missing';source.request.regions=['A:10-12'];
  h.context.requestAPI=async()=>source;h.run('api=requestAPI');
  await h.run('openProjectInput("a",true)');
  assert.equal(h.run('asset'),null);
  assert.match(h.element('design-source').innerHTML,/原始结构校验失败/);
  h.element('design-name').value='New project';
  assert.throws(()=>h.run('request("design")'),/先载入蛋白结构/);
});

test('project pages link to original-input and new-copy routes without loading full input during polling',async()=>{
  const h=harness(),calls=[];
  h.context.requestAPI=async key=>{calls.push(key);return job('a');};h.run('api=requestAPI');await h.run('openJob()');
  assert.match(h.element('job-detail').innerHTML,/href="#\/jobs\/a\/input">查看原始输入/);
  assert.match(h.element('job-detail').innerHTML,/href="#\/jobs\/a\/copy">基于此项目新建/);
  assert.deepEqual(calls,['jobs/a/overview']);
});
