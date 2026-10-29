"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const states = {queued:"等待启动",starting:"预检中",running:"运行中",collecting:"收集核验",succeeded:"产物已验证",failed:"失败",attention:"需要处理",cancelling:"正在取消",cancelled:"已取消",pending:"等待上游",submitted:"已提交",submitting:"提交确认中",blocked:"上游阻塞"};
const activeStates = new Set(["queued","starting","running","collecting","cancelling"]);
let config, asset, selected = new Set(), regions = [], anchor, designViewer, resultViewer;
let globalResources;
let pending, jobs = [], currentJob, currentResult = 0, monitorBusy = false, currentPage = "folding";
let toastTimer;
let jobsOffset=0, jobsTotal=0, jobsCounts={total:0,active:0,succeeded:0,problem:0}, listVersion=0;
let detailVersion=0, resultVersion=0;
let listAbort, detailAbort;
let resultColorMode="confidence";
let projectInput, inputVersion=0, structureVersion=0, designStructureText="";
function notify(message, error=false) {
  $("toast").textContent = message;
  $("toast").className = error ? "error" : "";
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").hidden = true, error ? 14000 : 5000);
}
async function api(path, data, options={}) {
  const response = await fetch("/api/" + path, data === undefined ? options : {...options,
    method:"POST", headers:{"Content-Type":"application/json","X-Antigen-Client":"local"},
    body:JSON.stringify(data)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
function on(id, event, handler) {
  $(id).addEventListener(event, async e => {
    try { await handler(e); } catch (error) { notify(error.message, true); }
  });
}
async function busy(button, action) {
  button.disabled = true;
  try { return await action(); } finally { button.disabled = false; }
}
function navigate(page, updateURL=true, keepInput=false) {
  if(!keepInput){inputVersion++;structureVersion++;leaveProjectInput();}
  if(page!==currentPage) {
    detailVersion++;resultVersion++;detailSignature="";
    detailAbort?.abort();listAbort?.abort();listVersion++;monitorBusy=false;
  }
  currentPage = page;
  document.querySelectorAll(".page").forEach(el => el.hidden = el.id !== "page-" + page);
  document.querySelectorAll(".nav").forEach(el => el.classList.toggle("active", el.dataset.page === (["project","result"].includes(page)?"monitor":page)));
  $("breadcrumb").textContent = {folding:"结构预测",design:"区域设计",monitor:"任务监控",project:"项目详情",result:"结构与指标",input:"原始输入"}[page];
  if(updateURL) window.history?.pushState(null,"",page==="project"?`#/jobs/${currentJob}`:`#/${page}`);
  if (page === "design" && designViewer) { designViewer.resize(); designViewer.render(); }
  if (page === "monitor") refreshJobs();
}
async function loadRoute() {
  const parts=(window.location?.hash||"#/folding").slice(2).split("/");
  if(parts[0]==="jobs"&&/^[a-f0-9]{32}$/.test(parts[1]||"")) {
    if(currentJob!==parts[1])$("job-detail").innerHTML="正在载入项目…";
    currentJob=parts[1];detailVersion++;resultVersion++;detailSignature="";
    if(parts.length===3&&["input","copy"].includes(parts[2])) {
      await openProjectInput(currentJob,parts[2]==="copy");
    } else if(parts.length===4&&parts[2]==="results") {
      navigate("result",false);
      const version=detailVersion,job=await api("jobs/"+currentJob+"/overview");
      if(version!==detailVersion||currentPage!=="result")return;
      await openResultPage(job,decodeURIComponent(parts[3]));
    } else {navigate("project",false);await openJob();}
  } else navigate(["folding","design","monitor"].includes(parts[0])?parts[0]:"monitor",false);
}
window.addEventListener("hashchange",()=>loadRoute().catch(e=>notify(e.message,true)));
on("refresh-project","click",()=>openJob());
document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
function gpuResourceLabel(target) {
  return target.label || `${target.host} / ${target.backend==="baremetal"?"裸金属 · S5000":target.resources.partition}`;
}
function executionState(task) {
  const native=task.backend==="baremetal"||task.observation?.backend==="baremetal";
  const state=task.observation?.state||task.execution_state||task.slurm_state||"—";
  return native?`裸金属 · ${state}${task.observation?.pid?` · PID ${task.observation.pid}`:""}`:state;
}
function nativeTask(task) {
  return task.backend==="baremetal"||task.observation?.backend==="baremetal"||task.route?.target?.backend==="baremetal";
}
function validTimestamp(value) { return typeof value==="number"&&Number.isFinite(value)&&value>=0; }
function formatElapsed(value) {
  if(typeof value!=="number")return typeof value==="string"&&value?value:"—";
  if(!Number.isFinite(value)||value<0)return "—";
  const ticks=Math.round(value*100),pad=n=>String(n).padStart(2,"0");
  return `${pad(Math.floor(ticks/360000))}:${pad(Math.floor(ticks/6000)%60)}:${pad(Math.floor(ticks/100)%60)}.${pad(ticks%100)}`;
}
function taskElapsed(task) {
  const observed=task.observation||{};
  let value=observed.elapsed;
  const terminal=["COMPLETED","FAILED","CANCELLED","TIMEOUT"].includes(observed.state||task.execution_state)
    ||["succeeded","failed","cancelled"].includes(task.state);
  if(nativeTask(task)&&terminal) {
    value=validTimestamp(observed.started_at)&&validTimestamp(observed.finished_at)
      ?observed.finished_at-observed.started_at:null;
  }
  return formatElapsed(value);
}
function nativeTimingDetails(task) {
  if(!nativeTask(task))return "";
  const observed=task.observation||{};
  const clock=value=>validTimestamp(value)?new Date(value*1000).toLocaleString("zh-CN",{hour12:false}):"—";
  const times=validTimestamp(observed.started_at)||validTimestamp(observed.finished_at)
    ?`<small>开始 ${esc(clock(observed.started_at))}<br>结束 ${esc(clock(observed.finished_at))}</small>`:"";
  return times+(observed.timing_error?`<small>${esc(observed.timing_error)}</small>`:"");
}
function resources(prefix) {
  if(!globalResources)return;
  const s=globalResources.settings;
  const targets=globalResources.gpu_catalog.filter(t=>s.gpu_targets.includes(t.id));
  if(globalResources.configured===false){
    $(prefix + "-resources").textContent="尚未配置计算后端。请在 configs/webapp.yaml 设置 server_profile，然后重启服务。";
    return;
  }
  if(!targets.length){
    $(prefix + "-resources").textContent=`后端 GPU：${globalResources.default_gpu_host||"由服务器配置指定"}；MSA：${globalResources.msa_catalog.map(t=>t.label).join(" · ")}。并发限制在任务监控页设置。`;
    return;
  }
  $(prefix + "-resources").innerHTML=`<details><summary>全局自动调度 · ${targets.length} 项 GPU 资源</summary><p>MSA：${s.msa_backends.map(esc).join(" · ")}<br>GPU：${targets.map(t=>`${esc(gpuResourceLabel(t))}${prefix==="design"&&!t.design?"（仅预测）":""}`).join(" · ")}</p></details>`;
}
function renderResourceSettings() {
  const s=globalResources.settings;
  $("resource-gpu-options").innerHTML='<b>GPU</b>'+globalResources.gpu_catalog.map(t=>`<label><input type="checkbox" data-resource="gpu" value="${esc(t.id)}" ${s.gpu_targets.includes(t.id)?"checked":""}> ${esc(gpuResourceLabel(t))}${!t.design?"（仅预测）":""}</label>`).join("");
  $("resource-msa-options").innerHTML='<b>CPU MSA</b>'+globalResources.msa_catalog.map(t=>`<label><input type="checkbox" data-resource="msa" value="${esc(t.id)}" ${s.msa_backends.includes(t.id)?"checked":""}> ${esc(t.label)}</label>`).join("");
  $("resource-queue-grace").value=s.queue_grace_seconds;
  $("resources-status").textContent="";
  $("save-resources").disabled=globalResources.configured===false;
  if(globalResources.configured===false)$("resources-status").textContent="配置计算后端后可管理资源。";
  resources("fold");resources("design");
}
const formFields={fold:["name","input","seeds","samples"],design:["name","backbones","sequences","temp","seed","seeds","samples"]};
function applyConfiguredDefaults(defaults) {
  // Configuration can arrive after a historical input. Update its saved draft,
  // never the original submission or the copy the user is already editing.
  const set=(prefix,key,value)=>{
    if(value===undefined)return;
    if(projectInput?.prefix===prefix)projectInput.backup.values[key]=String(value);
    else $(`${prefix}-${key}`).value=value;
  };
  for(const prefix of ["fold","design"]){
    if(defaults.seeds)set(prefix,"seeds",defaults.seeds.join(", "));
    set(prefix,"samples",defaults.samples);
  }
  for(const [key,setting] of [["backbones","backbones"],["sequences","sequences"],["temp","temperature"],["seed","seed"]])set("design",key,defaults[setting]);
  if(projectInput?.prefix==="design")projectInput.backup.method=Boolean(defaults.rfd3);
  else setDesignMethod(Boolean(defaults.rfd3));
}
function originalForm(prefix) {return Boolean(projectInput?.prefix===prefix&&!projectInput.editing);}
function captureForm(prefix) {
  const snapshot={values:Object.fromEntries(formFields[prefix].map(key=>[key,$(`${prefix}-${key}`).value]))};
  if(prefix==="design")Object.assign(snapshot,{asset,text:designStructureText,method:diffusion(),
    selected:[...selected],regions:regions.map(r=>({...r,indices:[...r.indices]})),anchor});
  return snapshot;
}
function restoreForm(prefix,snapshot) {
  for(const [key,value] of Object.entries(snapshot.values))$(`${prefix}-${key}`).value=value;
  if(prefix==="fold"){inputStats();return;}
  setDesignMethod(snapshot.method);
  if(snapshot.asset)renderStructure(snapshot.asset,snapshot.text);
  else {
    asset=null;designViewer=null;designStructureText="";
    $("design-viewer").innerHTML='<div class="viewer-empty"><h3>未载入结构</h3><p>导入 PDB / mmCIF，或载入 1UBQ 示例。</p></div>';
    $("structure-title").textContent="结构";$("residue-count").textContent="0 个残基";
    $("sequence-panel").textContent="载入结构后显示序列。";$("structure-warnings").textContent="";
  }
  selected=new Set(snapshot.selected);regions=snapshot.regions;anchor=snapshot.anchor;
  if(asset){renderSequence();styleDesign();}renderRegions();
}
function setDesignMethod(enabled) {
  document.querySelectorAll('input[name="method"]').forEach(input=>input.checked=input.value===(enabled?"rfd3":"mpnn"));
  $("design-backbones").disabled=!enabled;
}
function lockInputForm(prefix) {
  const locked=originalForm(prefix);
  formFields[prefix].forEach(key=>$(`${prefix}-${key}`).readOnly=locked);
  for(const key of ["upload","file","example"])$(`${prefix}-${key}`).disabled=locked;
  $(`${prefix}-preview`).hidden=locked;
  if(prefix==="design") {
    document.querySelectorAll('input[name="method"]').forEach(input=>input.disabled=locked);
    $("region-input").readOnly=locked;
    for(const id of ["add-region","clear-selection"])$(id).disabled=locked;
    $("design-backbones").disabled=locked||!diffusion();
    if(asset)renderSequence();renderRegions();
  }
}
function leaveProjectInput() {
  if(!projectInput)return;
  const {prefix,backup}=projectInput;projectInput=null;pending=null;
  $(`${prefix}-source`).hidden=true;
  restoreForm(prefix,backup);lockInputForm(prefix);
}
function showInputBanner() {
  const {prefix,source,editing}=projectInput,panel=$(`${prefix}-source`);
  panel.hidden=false;
  panel.innerHTML=`<a class="text-button" href="#/jobs/${source.id}">← 返回项目详情</a>
    <div class="card-heading"><h2>${editing?"新建副本":"原始输入"} · ${esc(source.name)}</h2>${editing?`<a class="secondary" href="#/jobs/${source.id}/input">查看原始输入</a>`:`<button class="primary" id="${prefix}-source-copy" type="button">修改并建立新项目</button>`}</div>
    <p class="small-note">${editing?"修改输入和参数，填写新名称后提交。新项目使用当前全局资源配置。":"首次提交的输入和参数，只读。"}</p>
    ${source.download_url?`<a class="text-button" href="${esc(source.download_url)}" download="${esc(source.asset?.filename||"input.cif")}">下载原始结构 · ${esc(source.asset?.filename)}</a>`:""}
    ${source.asset_error?`<p class="error-box">${esc(source.asset_error)}；新建时可重新上传结构并选择区间。</p>`:""}
    <details class="help"><summary>原始提交记录（JSON）</summary><pre class="review-json">${esc(JSON.stringify(source.request,null,2))}</pre></details>`;
  if(!editing)$(`${prefix}-source-copy`).addEventListener("click",beginProjectCopy);
  $("breadcrumb").textContent=editing?"新建副本":"原始输入";
  lockInputForm(prefix);
}
function beginProjectCopy() {
  if(!projectInput||projectInput.editing)return;
  projectInput.editing=true;pending=null;inputVersion++;
  $(`${projectInput.prefix}-name`).value="";
  window.history?.pushState(null,"",`#/jobs/${projectInput.source.id}/copy`);
  showInputBanner();$(`${projectInput.prefix}-name`).focus();
}
async function openProjectInput(jobId,editing=false) {
  navigate("input",false);
  const version=++inputVersion,current=()=>version===inputVersion&&jobId===currentJob&&currentPage==="input";
  $("input-back").href=`#/jobs/${jobId}`;$("input-loading").textContent="正在载入原始输入…";
  try {
    const source=await api(`jobs/${jobId}/input`);
    if(!current())return;
    const data=source.request,prefix=data.mode==="folding"?"fold":data.mode==="design"?"design":null;
    if(!prefix)throw new Error("无法识别原始项目类型");
    let text="";
    if(source.asset) {
      const response=await fetch(`/api/jobs/${jobId}/input/structure`);
      if(!response.ok)throw new Error(`原始结构读取失败：HTTP ${response.status}`);
      text=await response.text();
    }
    if(!current())return;
    projectInput={prefix,source,editing,backup:captureForm(prefix)};pending=null;
    $(`${prefix}-name`).value=editing?"":data.name;
    $(`${prefix}-seeds`).value=(data.seeds??[101]).join(", ");
    $(`${prefix}-samples`).value=String(data.samples??1);
    if(prefix==="fold") {$("fold-input").value=data.input??"";inputStats();}
    else {
      setDesignMethod(data.rfd3??false);
      for(const [key,value] of Object.entries({backbones:data.backbones??1,sequences:data.sequences??1,temp:data.temperature??0.1,seed:data.seed??42}))$(`design-${key}`).value=String(value);
      if(source.asset)renderStructure(source.asset,text);
      else {
        asset=null;designViewer=null;designStructureText="";
        $("structure-title").textContent="原始结构不可用";$("residue-count").textContent="—";
        $("design-viewer").textContent=source.asset_error||"原始结构不可用";
        $("sequence-panel").textContent="原始残基映射不可用。";$("structure-warnings").textContent="";
      }
      selected=new Set();anchor=undefined;
      regions=(data.regions??[]).map(spec=>{
        const region=typeof spec==="string"?spec:spec.region,indices=asset?regionIndices(region):[];
        indices.forEach(i=>selected.add(i));
        const length=typeof spec==="object"?spec.length:null;
        return {region,indices,min:length?.[0]??indices.length,max:length?.[1]??indices.length};
      });
      if(asset){renderSequence();styleDesign();}renderRegions();
    }
    navigate(prefix==="fold"?"folding":"design",false,true);showInputBanner();
  } catch(error) {
    if(!current())return;
    $("input-loading").textContent="原始输入载入失败："+error.message;
    throw error;
  }
}
function seeds(prefix) {
  const values = $(prefix + "-seeds").value.split(/[,，\s]+/).filter(Boolean).map(Number);
  if (!values.length || values.some(v => !Number.isInteger(v) || v < 0)) throw new Error("种子需为非负整数，以逗号分隔");
  return values;
}
function request(prefix) {
  if(originalForm(prefix))throw new Error("请先点击“修改并建立新项目”");
  if(!$(prefix + "-name").value.trim()) throw new Error("请填写项目名称");
  const data = {name:$(prefix + "-name").value.trim(),
                mode:prefix === "fold" ? "folding" : "design", seeds:seeds(prefix),
                samples:Number($(prefix + "-samples").value)};
  if (prefix === "fold") data.input = $("fold-input").value;
  else {
    if (!asset) throw new Error("请先载入蛋白结构");
    data.asset = asset.id;
    data.rfd3 = diffusion();
    data.regions = regions.map(r => ({region:r.region, ...(data.rfd3 ? {length:[r.min,r.max]} : {})}));
    data.backbones = Number($("design-backbones").value);
    data.sequences = Number($("design-sequences").value);
    data.temperature = Number($("design-temp").value);
    data.seed = Number($("design-seed").value);
  }
  if(projectInput?.prefix===prefix&&projectInput.editing)data.source_job=projectInput.source.id;
  return data;
}
async function preview(prefix) {
  const data = request(prefix),version=inputVersion,page=currentPage;
  const result = await api("preview", data);
  if(version!==inputVersion||page!==currentPage)return;
  pending = {...data, id:crypto.randomUUID().replaceAll("-","")};
  // Static format restrictions are already in the input help and full preview details.
  const formatNotes=new Set([
    "RNA/DNA do not receive an MSA from the configured protein ColabFold search.",
    "Custom CCD, templates and supplied MSA fields are rejected rather than ignored."
  ]);
  $("preview-body").innerHTML = `<p><strong>${esc(data.name)}</strong> · 全局自动调度 · Protenix v2</p>
    ${data.source_job?`<p class="small-note">来源项目：${esc(projectInput.source.name)}</p>`:""}
    <div class="metric-grid"><div class="metric"><small>预计预测数</small><b>${result.expected_predictions}</b></div>
    <div class="metric"><small>CPU 主机</small><b style="font-size:15px">${esc(result.cpu_host)}</b></div>
    <div class="metric"><small>MSA 资源</small><b style="font-size:15px">全局自动分配</b><small>确认取消后转投</small></div>
    <div class="metric"><small>推理 GPU</small><b style="font-size:15px">${result.gpu_routing?.enabled?"自动调度":esc(result.gpu_host)}</b></div></div>
    <div class="review-stages">${result.stages.map(s => `<span>${esc(s.toUpperCase())}</span>`).join(" → ")}</div>
    ${result.selected ? `<p class="small-note">设计残基：${result.selected.length}；骨架长度：${esc(result.backbone_lengths.join(", "))}</p>` : ""}
    <p class="small-note">提交后将占用计算资源；关闭页面不停止作业。</p>
    ${result.warnings.filter(w=>!formatNotes.has(w)).map(w => `<p class="small-note">${esc(w)}</p>`).join("")}
    <details><summary class="small-note">输入转换与资源详情</summary><pre class="review-json">${esc(JSON.stringify(result,null,2))}</pre></details>`;
  $("preview-dialog").showModal();
}
on("fold-preview","click",e => busy(e.currentTarget,() => preview("fold")));
on("design-preview","click",e => busy(e.currentTarget,() => preview("design")));
on("close-preview","click",() => $("preview-dialog").close());
on("back-preview","click",() => $("preview-dialog").close());
on("submit-job","click",e => busy(e.currentTarget,async () => {
  const result = await api("jobs",pending);
  $("preview-dialog").close();
  notify("项目已提交，等待预检和调度。");
  currentJob = result.id;
  navigate("project");await openJob();
}));
on("fold-upload","click",() => $("fold-file").click());
on("fold-file","change",async e => {
  const file = e.target.files[0];
  if (!file) return;
  if (file.size > 4 * 1024**2) throw new Error("输入文件不能超过 4 MiB");
  $("fold-input").value = await file.text();
  inputStats();
});
function inputStats() {
  const text = $("fold-input").value.trim();
  if (!text) $("input-stat").textContent = "等待输入";
  else if (/^[{[]/.test(text)) {
    try { const value=JSON.parse(text); $("input-stat").textContent=`JSON · ${Array.isArray(value) ? value.length : 1} 个任务 · 提交前校验`; }
    catch { $("input-stat").textContent="JSON 尚不完整"; }
  } else {
    const lines=text.split("\n"), count=lines.filter(l=>l.startsWith(">")).length || 1;
    $("input-stat").textContent=`${count} 条独立序列 · ${lines.filter(l=>!l.startsWith(">")).join("").replace(/\s/g,"").length} 个残基`;
  }
}
on("fold-input","input",inputStats);
on("fold-example","click",e => busy(e.currentTarget,async () => {
  const example = await api("benchmark",{});
  $("fold-input").value = ">1UBQ_human_ubiquitin\n" + example.residues.map(r=>r.amino_acid).join("");
  inputStats();
}));
on("design-upload","click",() => $("design-file").click());
on("design-example","click",e => busy(e.currentTarget,async () => loadStructure(await api("benchmark",{}))));
on("design-file","change",async e => {
  const file=e.target.files[0]; if(!file) return;
  if(file.size>20*1024**2) throw new Error("结构文件不能超过 20 MiB");
  await loadStructure(await api("assets",{filename:file.name,content:await file.text()}));
});
async function loadStructure(value) {
  const version=++structureVersion;
  const response=await fetch(`/api/assets/${value.id}/structure`);
  if(!response.ok)throw new Error(`结构读取失败：HTTP ${response.status}`);
  const text=await response.text();
  if(version!==structureVersion)return;
  renderStructure(value,text);
}
function renderStructure(value,text) {
  asset=value; selected=new Set(); regions=[]; anchor=undefined;
  designStructureText=text;sequenceDrag=null;
  $("surface-status").textContent="悬停查看原始编号；Shift + 点击选择区间。";
  $("structure-title").textContent=asset.filename;
  $("residue-count").textContent=`${asset.residues.length} 个残基`;
  $("structure-warnings").textContent=asset.warnings.join(" ");
  $("design-viewer").replaceChildren();
  try {
    designViewer=$3Dmol.createViewer($("design-viewer"),{backgroundColor:"#edf2ec",antialias:true});
    designViewer.addModel(text,"pdb");
    designViewer.setClickable({},true,(atom,viewer,event)=>{
      const residue=asset.residues.find(r=>r.normalized_chain===atom.chain && r.position===atom.resi);
      if(residue) pickResidue(residue.index,Boolean(event?.shiftKey));
    });
    designViewer.setHoverable({},true,atom=>{
      const r=asset.residues.find(r=>r.normalized_chain===atom.chain && r.position===atom.resi);
      $("hover-residue").textContent=r ? designResidueInfo(r) : "";
    },()=>{$("hover-residue").textContent="点击残基选定 · 拖动旋转 · 滚轮缩放";});
    styleDesign(); designViewer.zoomTo(); designViewer.render();
  } catch(error) {
    designViewer=null;
    $("design-viewer").innerHTML='<div class="viewer-empty"><h3>WebGL 不可用</h3><p>可通过序列或原始编号选择区间。</p></div>';
    notify(error.message,true);
  }
  renderSequence(); renderRegions();
}
function diffusion() { return document.querySelector('input[name="method"]:checked').value==="rfd3"; }
const aminoAcids = {ALA:"A",ARG:"R",ASN:"N",ASP:"D",CYS:"C",GLN:"Q",GLU:"E",GLY:"G",HIS:"H",ILE:"I",LEU:"L",LYS:"K",MET:"M",PHE:"F",PRO:"P",SER:"S",THR:"T",TRP:"W",TYR:"Y",VAL:"V",MSE:"M",SEC:"U",PYL:"O"};
function aminoLetter(name) { return aminoAcids[name] || (/^[A-Z]$/.test(name) ? name : "X"); }
function designResidueInfo(r) {
  const name=Object.keys(aminoAcids).find(key=>aminoAcids[key]===r.amino_acid) || "未知";
  return `${r.original_id} · ${name} (${r.amino_acid}) · 链 ${r.chain} · 原始编号 ${r.number}${r.insertion} · 链内位置 ${r.position} · 编号来源 ${r.numbering_source} · rSASA ${Number.isFinite(r.rsasa)?r.rsasa.toFixed(4):"未计算"}`;
}
function styleDesign() {
  if(!designViewer) return;
  designViewer.setStyle({},{cartoon:{color:"#759daa"}});
  for(const index of selected) {
    const r=asset.residues[index];
    designViewer.setStyle({chain:r.normalized_chain,resi:r.position},{cartoon:{color:"#ed9a51"}});
  }
  designViewer.render();
}
function renderSequence() {
  const chains=[...new Set(asset.residues.map(r=>r.chain))];
  $("sequence-panel").innerHTML=chains.map(chain=>`<div class="chain-header">链 ${esc(chain)} · ${asset.residues.filter(r=>r.chain===chain).length} 个残基</div><div class="residue-wrap">${asset.residues.filter(r=>r.chain===chain).map(r=>`<button class="residue ${selected.has(r.index)?"selected":""}" data-index="${r.index}" aria-pressed="${selected.has(r.index)}" aria-disabled="${Boolean(originalForm("design"))}" aria-label="${esc(designResidueInfo(r))}" title="${esc(designResidueInfo(r))}">${esc(r.amino_acid)}</button>`).join("")}</div>`).join("");
  $("sequence-panel").querySelectorAll("button").forEach(button=>{
    button.addEventListener("click",e=>{if(e.detail && performance.now()<suppressSequenceClickUntil)return;pickResidue(Number(button.dataset.index),e.shiftKey);});
    for(const event of ["mouseenter","focus"]) button.addEventListener(event,()=>{$("hover-residue").textContent=designResidueInfo(asset.residues[Number(button.dataset.index)]);});
  });
}
let sequenceDrag=null,suppressSequenceClickUntil=0;
$("sequence-panel").addEventListener("pointerdown",event=>{
  const button=event.target.closest("button[data-index]");
  if(!button||event.button!==0||!asset||originalForm("design"))return;
  suppressSequenceClickUntil=0;
  sequenceDrag={id:event.pointerId,start:Number(button.dataset.index),base:new Set(selected),moved:false};
});
$("sequence-panel").addEventListener("pointermove",event=>{
  if(!sequenceDrag||sequenceDrag.id!==event.pointerId)return;
  const button=document.elementFromPoint(event.clientX,event.clientY)?.closest("#sequence-panel button[data-index]");
  if(!button)return;
  const end=Number(button.dataset.index),start=sequenceDrag.start;
  if(asset.residues[start].chain!==asset.residues[end].chain)return;
  if(end===start&&!sequenceDrag.moved)return;
  sequenceDrag.moved=true;
  $("sequence-panel").setPointerCapture(event.pointerId);
  selected=new Set(sequenceDrag.base);
  for(let i=Math.min(start,end);i<=Math.max(start,end);i++)selected.add(i);
  $("sequence-panel").querySelectorAll("button[data-index]").forEach(b=>{
    const active=selected.has(Number(b.dataset.index));b.classList.toggle("selected",active);b.setAttribute("aria-pressed",String(active));
  });
  event.preventDefault();
});
function finishSequenceDrag(event) {
  if(!sequenceDrag||event.pointerId!==sequenceDrag.id)return;
  const drag=sequenceDrag;sequenceDrag=null;
  if($("sequence-panel").hasPointerCapture(event.pointerId))$("sequence-panel").releasePointerCapture(event.pointerId);
  if(event.type==="pointercancel")selected=drag.base;
  if(drag.moved){
    suppressSequenceClickUntil=performance.now()+250;anchor=drag.start;
    deriveRegions();renderSequence();renderRegions();styleDesign();
  }
}
window.addEventListener("pointerup",finishSequenceDrag);
window.addEventListener("pointercancel",finishSequenceDrag);
function pickResidue(index, range=false) {
  if(originalForm("design"))return;
  const residue=asset.residues[index];
  if(range && anchor!==undefined && asset.residues[anchor].chain===residue.chain) {
    for(let i=Math.min(anchor,index);i<=Math.max(anchor,index);i++) selected.add(i);
  } else if(selected.has(index)) selected.delete(index);
  else selected.add(index);
  anchor=index;
  deriveRegions(); renderSequence(); renderRegions(); styleDesign();
}
function deriveRegions() {
  const groups=[];
  for(const index of [...selected].sort((a,b)=>a-b)) {
    const last=groups.at(-1);
    if(last && index===last.at(-1)+1 && asset.residues[index].chain===asset.residues[last[0]].chain) last.push(index);
    else groups.push([index]);
  }
  regions=groups.map(indices=>{
    const first=asset.residues[indices[0]],last=asset.residues[indices.at(-1)];
    const region=`${first.chain}:${first.number}${first.insertion}-${last.number}${last.insertion}`;
    const old=regions.find(r=>r.region===region);
    return {region,indices,min:old?.min??indices.length,max:old?.max??indices.length};
  });
}
function renderRegions() {
  const locked=originalForm("design");
  $("exclude-buried").disabled=locked || !asset || !selected.size || Boolean(asset.surfacePending);
  $("selected-count").textContent=`${regions.length} 个区间 · ${selected.size} 个残基`;
  $("region-list").innerHTML=regions.length ? regions.map((r,i)=>`<div class="region-row"><code>${esc(r.region)} <small>(${asset?r.indices.length+" aa":"映射不可用"})</small></code><label>输出长度</label><input type="number" min="1" max="5000" value="${r.min}" data-region="${i}" data-bound="min" aria-label="${esc(r.region)} 最小长度" ${diffusion()&&!locked?"":"disabled"}><span>–</span><input type="number" min="1" max="5000" value="${r.max}" data-region="${i}" data-bound="max" aria-label="${esc(r.region)} 最大长度" ${diffusion()&&!locked?"":"disabled"}><button class="icon-button" data-remove="${i}" aria-label="移除区间" ${locked?"disabled":""}>×</button></div>`).join("") : '<p class="muted">未选择设计区间。</p>';
  $("region-list").querySelectorAll("input").forEach(input=>input.addEventListener("change",()=>regions[Number(input.dataset.region)][input.dataset.bound]=Number(input.value)));
  $("region-list").querySelectorAll("button").forEach(button=>button.addEventListener("click",()=>{
    regions[Number(button.dataset.remove)].indices.forEach(i=>selected.delete(i));
    deriveRegions(); renderSequence(); renderRegions(); styleDesign();
  }));
}
function regionIndices(raw) {
  if(!asset) throw new Error("请先载入结构");
  const match=raw.match(/^([^:]+):(-?\d+[A-Za-z]?)(?:-(-?\d+[A-Za-z]?))?$/);
  if(!match) throw new Error("编号格式如 A:10-12 或 A:11A-15");
  const records=asset.residues.filter(r=>r.chain===match[1]);
  const a=records.find(r=>`${r.number}${r.insertion}`===match[2]);
  const b=records.find(r=>`${r.number}${r.insertion}`===(match[3]||match[2]));
  if(!a||!b||a.index>b.index) throw new Error("区间端点不存在或编号顺序颠倒");
  return Array.from({length:b.index-a.index+1},(_,i)=>a.index+i);
}
on("add-region","click",()=>{
  if(originalForm("design"))return;
  for(const index of regionIndices($("region-input").value.trim()))selected.add(index);
  deriveRegions(); renderSequence(); renderRegions(); styleDesign(); $("region-input").value="";
});
on("clear-selection","click",()=>{selected.clear();regions=[];anchor=undefined;if(asset){renderSequence();renderRegions();styleDesign();}});
on("exclude-buried","click",async e=>{
  if(!asset || !selected.size) return;
  const currentAsset=asset;
  const button=e.currentTarget;
  currentAsset.surfacePending=true;
  await busy(button,async()=>{
    $("surface-status").textContent="正在计算 rSASA…";
    if(!currentAsset.surface) {
      const result=await api(`assets/${currentAsset.id}/surface`,{});
      if(asset!==currentAsset) return;
      if(result.residues.length!==asset.residues.length || result.residues.some((r,i)=>r.index!==i || r.original_id!==asset.residues[i].original_id || !Number.isFinite(r.rsasa))) throw new Error("rSASA 残基映射不匹配");
      result.residues.forEach((r,i)=>asset.residues[i].rsasa=r.rsasa);
      asset.surface=result;
    }
    if(asset!==currentAsset) return;
    const removed=[...selected].filter(index=>asset.residues[index].rsasa<0.2);
    removed.forEach(index=>selected.delete(index));
    anchor=undefined;
    deriveRegions(); renderSequence(); renderRegions(); styleDesign();
    $("surface-status").textContent=`已移除 ${removed.length} 个位点，保留 ${selected.size} 个（rSASA ≥ 0.2，按全部蛋白链计算）。${removed.length?"新区间长度已重置为残基数，请核对。":""}`;
  }).catch(error=>{if(asset===currentAsset) $("surface-status").textContent="rSASA 计算失败，选区未改变。";throw error;})
    .finally(()=>{currentAsset.surfacePending=false;renderRegions();});
});
on("reset-view","click",()=>{designViewer?.zoomTo();designViewer?.render();});
document.querySelectorAll('input[name="method"]').forEach(input=>input.addEventListener("change",()=>{
  $("design-backbones").disabled=!diffusion();
  if(!diffusion()) { $("design-backbones").value="1"; regions.forEach(r=>{r.min=r.max=r.indices.length;}); }
  renderRegions();
}));
function badge(state) { return `<span class="badge ${esc(state)}">${esc(states[state]||state)}</span>`; }
function date(value) { return value ? new Date(value).toLocaleString("zh-CN",{hour12:false}) : "—"; }
function renderJobs() {
  $("job-count").textContent=jobsCounts.total;
  $("job-count").hidden=false;
  const counts=[["全部运行",jobsCounts.total],["进行中",jobsCounts.active],["已验证完成",jobsCounts.succeeded],["需要处理",jobsCounts.problem]];
  $("job-stats").innerHTML=counts.map(([title,count])=>`<div class="stat"><small>${title}</small><strong>${count}<span class="unit">RUNS</span></strong></div>`).join("");
  const filtered=jobs;
  $("jobs-body").innerHTML=filtered.map(job=>{
    const complete=job.completed_tasks,total=job.task_count;
    const archive=!job.unavailable&&["succeeded","failed","cancelled"].includes(job.state)
      ?`<button class="text-button" data-archive="${job.id}" data-archived="${Boolean(job.archived)}">${job.archived?"取消归档":"归档"}</button>`:"";
    return `<tr><td><strong>${esc(job.name)}</strong><small>${job.mode.toUpperCase()} · ${job.id.slice(0,8)}</small></td><td>${esc(job.profile)}</td><td>${complete} / ${total||"—"} 阶段<div class="progress"><i style="width:${total?100*complete/total:0}%"></i></div></td><td>${badge(job.state)}</td><td><small>${date(job.created_at)}</small></td><td><div class="button-row"><button class="text-button" data-open="${job.id}">查看 →</button>${archive}</div></td></tr>`;
  }).join("");
  $("jobs-empty").hidden=filtered.length>0;
  $("jobs-prev").disabled=jobsOffset===0;
  $("jobs-next").disabled=jobsOffset+25>=jobsTotal;
  $("jobs-page").textContent=`${jobsTotal?jobsOffset+1:0}–${Math.min(jobsOffset+25,jobsTotal)} / ${jobsTotal}`;
  $("jobs-body").querySelectorAll("[data-open]").forEach(button=>button.addEventListener("click",async()=>{
    currentJob=button.dataset.open; currentResult=0;$("job-detail").innerHTML="正在载入项目…";
    resultVersion++;detailSignature="";
    try { navigate("project");await openJob(); }
    catch(error) { notify(error.message,true); }
  }));
  $("jobs-body").querySelectorAll("[data-archive]").forEach(button=>button.addEventListener("click",()=>archiveFromList(button)));
}
async function archiveFromList(button) {
  await busy(button,async()=>{
    try {
      const archived=button.dataset.archived==="true";
      await api(`jobs/${button.dataset.archive}/${archived?"unarchive":"archive"}`,{});
      await refreshJobs(true);
      notify(archived?"已取消归档":"已归档，可在已归档列表恢复");
    } catch(error) {notify(error.message,true);}
  });
}
async function refreshJobs(force=false) {
  if(monitorBusy&&!force) return;
  const version=++listVersion;
  listAbort?.abort();listAbort=new AbortController();
  monitorBusy=true;
  $("jobs-page").textContent="正在加载…";
  try {
    const query=new URLSearchParams({archive:$("filter-archive").value,profile:$("filter-profile").value,state:$("filter-state").value,offset:jobsOffset,limit:25});
    const page=await api("jobs?"+query,undefined,{signal:listAbort.signal});
    if(version!==listVersion)return;
    if(jobsOffset>0&&jobsOffset>=page.total){jobsOffset=page.total?Math.floor((page.total-1)/25)*25:0;return refreshJobs(true);}
    jobs=page.items;jobsTotal=page.total;jobsCounts=page.counts;renderJobs();
    $("connection").textContent="本地服务已连接";
  }
  catch(error) {if(error.name!=="AbortError"&&version===listVersion){$("connection").textContent="连接中断 · 显示上次快照";$("jobs-page").textContent="加载失败，请刷新重试";notify(error.message,true);}}
  finally {if(version===listVersion)monitorBusy=false;}
}
on("refresh-jobs","click",()=>refreshJobs(true));
for(const id of ["filter-archive","filter-profile","filter-state"])on(id,"change",()=>{jobsOffset=0;return refreshJobs(true);});
on("jobs-prev","click",()=>{jobsOffset=Math.max(0,jobsOffset-25);return refreshJobs(true);});
on("jobs-next","click",()=>{jobsOffset+=25;return refreshJobs(true);});
function msaPoolLabel(value) {
  return ({queued:"排队",running:"运行中",succeeded:"已验证",failed:"失败",cancelled:"已取消",staging:"暂存索引",verifying:"校验索引",ready:"索引就绪",serving:"处理请求",closing:"释放资源",closed:"已释放"})[value] || value;
}
function msaTransferLabel(phase) {
  return ({preparing:"准备输入",uploading:"传输输入",ready:"输入已送达",exporting:"整理结果",returning:"回传结果",validating:"校验结果",verified:"回传已校验"})[phase] || phase;
}
let detailSignature="";
let displayedJob, taskOffset=0, resultOffset=0;
const detailPageSize=50;
const extraVersions={logs:0,scheduling:0};
function pager(kind,offset,total) {
  if(total<=detailPageSize)return "";
  return `<div class="button-row detail-pager"><button class="secondary" data-page="${kind}" data-offset="${Math.max(0,offset-detailPageSize)}" ${offset===0?"disabled":""}>上一页</button><span role="status">${offset+1}–${Math.min(offset+detailPageSize,total)} / ${total}</span><button class="secondary" data-page="${kind}" data-offset="${offset+detailPageSize}" ${offset+detailPageSize>=total?"disabled":""}>下一页</button></div>`;
}
let renameJobId;
function editName(job) {
  renameJobId=job.id;
  $("rename-name").value=job.name;
  $("rename-error").textContent="";
  $("rename-dialog").showModal();
  $("rename-name").focus();
}
on("cancel-rename","click",()=>$("rename-dialog").close());
on("rename-form","submit",async e=>{
  e.preventDefault();
  await busy($("save-rename"),async()=>{
    try {
      const name=$("rename-name").value.trim();
      if(!name||name.length>120)throw new Error("项目名称需为 1–120 个字符");
      await api(`jobs/${renameJobId}/rename`,{name});
      $("rename-dialog").close();
      detailSignature="";
      await openJob();
      notify("项目名称已保存");
    } catch(error) {$("rename-error").textContent=error.message;}
  });
});
const detailPending=new Map();
async function openJob() {
  const jobId=currentJob;
  if(detailPending.has(jobId)&&!detailAbort?.signal.aborted)return detailPending.get(jobId);
  const pending=renderJob(jobId);detailPending.set(jobId,pending);
  try{return await pending;}finally{if(detailPending.get(jobId)===pending)detailPending.delete(jobId);}
}
async function renderJob(jobId) {
  const version=++detailVersion;
  detailAbort?.abort();detailAbort=new AbortController();
  let job;
  try {job=await api("jobs/"+jobId+"/overview",undefined,{signal:detailAbort.signal});}
  catch(error) {if(error.name==="AbortError")return;throw error;}
  if(version!==detailVersion||jobId!==currentJob||currentPage!=="project")return;
  if(displayedJob?.id!==job.id){taskOffset=0;resultOffset=0;}
  displayedJob=job;
  paintJob(job);
}
function paintJob(job) {
  taskOffset=Math.min(taskOffset,Math.max(0,Math.floor((job.tasks.length-1)/detailPageSize)*detailPageSize));
  resultOffset=Math.min(resultOffset,Math.max(0,Math.floor((job.results.length-1)/detailPageSize)*detailPageSize));
  // Avoid resetting log selection on unchanged polling snapshots.
  const signature=JSON.stringify([job,taskOffset,resultOffset]);
  if(signature===detailSignature) return;
  const expanded=Boolean($("task-scheduling")?.open);
  const logsExpanded=Boolean($("task-logs")?.open);
  detailSignature=signature;
  resultVersion++;
  const canAct=!job.history_only&&!job.unavailable&&!job.initializing&&!job.archived&&["failed","cancelled","attention"].includes(job.state), active=!job.unavailable&&!job.history_only&&activeStates.has(job.state);
  $("job-detail").hidden=false;
  $("job-detail").innerHTML=`<div class="card detail-card"><div class="card-heading"><h2>${esc(job.name)} ${badge(job.state)}</h2><div class="button-row">${canAct?'<button class="secondary" data-action="resume">恢复检查</button><button class="secondary" data-action="retry">重试失败阶段</button>':""}${!job.history_only&&!job.unavailable&&(active||job.state==="attention")?'<button class="secondary danger" data-action="cancel">取消运行</button>':""}${job.download_ready?`<a class="secondary" href="/api/jobs/${job.id}/download">下载完整结果 ↓</a>`:""}</div></div>
    <div class="detail-meta">创建：${date(job.created_at)} · 最近调度查询：${date(job.observed_at)}<br>运行 ${esc(job.id)} · ${esc(job.profile)} · ${esc(job.verification||"结果待校验")}</div>
    <div class="button-row project-input-actions"><a class="secondary" href="#/jobs/${job.id}/input">查看原始输入</a><a class="secondary" href="#/jobs/${job.id}/copy">基于此项目新建</a></div>
    ${job.history_only?'<p class="small-note">已迁移的历史记录，不支持恢复或重试；可基于原始输入新建项目。</p>':""}
    ${job.error?`<div class="error-box">${esc(job.error)}</div>`:""}${job.monitor_error?`<div class="error-box">监控更新失败，以下为上次快照：${esc(job.monitor_error)}</div>`:""}
    <div class="table-scroll"><table><thead><tr><th>阶段 / 任务</th><th>主机 / job ID</th><th>本地校验</th><th>执行状态</th><th>资源 / 用时</th><th>尝试</th></tr></thead><tbody>${job.tasks.slice(taskOffset,taskOffset+detailPageSize).map(t=>`<tr><td>${t.stage.toUpperCase()}<small>${esc(t.id)}</small></td><td>${esc(t.host)}<small>${esc(t.job_id||"尚未提交")}</small></td><td>${badge(t.state)}${t.pool?`<small>共享索引池 · 请求 ${esc(msaPoolLabel(t.observation?.pool_state||t.pool_state||"queued"))} · 池 ${esc(msaPoolLabel(t.observation?.pool_phase||t.pool_phase||"queued"))}</small>`:""}${t.transfer_phase?`<small>MSA 交接：${esc(msaTransferLabel(t.transfer_phase))}</small>`:""}${t.transfer_error?`<small>${esc(t.transfer_error)}</small>`:""}${t.error?`<small>${esc(t.error)}</small>`:""}</td><td>${esc(executionState(t))}${t.pool?"<small>共享分配状态</small>":""}${t.observation?.error?`<small>${esc(t.observation.error)}</small>`:""}</td><td>${esc(nativeTask(t)?"裸金属":t.observation?.partition||"—")}<small>${esc(t.observation?.cpus||"—")} CPU · ${esc(t.observation?.memory||"—")} · ${esc(taskElapsed(t))}</small>${nativeTimingDetails(t)}</td><td>${t.attempts}</td></tr>`).join("")}</tbody></table></div>
    ${pager("tasks",taskOffset,job.tasks.length)}
    <details class="help" id="task-scheduling"><summary>子任务调度详情</summary><div id="scheduling-content"></div></details>
    <details class="help" id="task-logs"><summary>控制器与阶段日志</summary><div id="logs-content"></div></details>
    <div class="card-heading" style="margin-top:28px"><h2>已完成结果 <span class="tag">${job.results.length}</span></h2></div><div class="table-scroll"><table><thead><tr><th>预测结果</th><th>Seed / Sample</th><th></th></tr></thead><tbody>${job.results.slice(resultOffset,resultOffset+detailPageSize).map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.seed)} / ${esc(r.sample)}</td><td><a class="text-button" href="#/jobs/${job.id}/results/${encodeURIComponent(r.id)}">查看结构与指标 →</a></td></tr>`).join("")||'<tr><td colspan="3">暂无已完成并通过校验的预测。</td></tr>'}</tbody></table></div>${pager("results",resultOffset,job.results.length)}</div>`;
  for(const [kind,open] of [["scheduling",expanded],["logs",logsExpanded]]) {
    const section=$("task-"+kind);
    section.open=open;
    section.addEventListener("toggle",()=>{
      if(section.open){if(!$(kind+"-content").textContent)loadExtras(kind);}
      else {extraVersions[kind]++;$(kind+"-content").textContent="";}
    });
    if(open)loadExtras(kind);
  }
  $("job-detail").querySelectorAll("[data-page]").forEach(button=>button.addEventListener("click",()=>{
    if(button.dataset.page==="tasks")taskOffset=Number(button.dataset.offset);
    else resultOffset=Number(button.dataset.offset);
    paintJob(displayedJob);
  }));
  if(!job.unavailable) {
    const renameButton=document.createElement("button");renameButton.className="secondary";
    renameButton.textContent="修改名称";
    renameButton.addEventListener("click",()=>editName(job));
    $("job-detail").querySelector(".card-heading .button-row").append(renameButton);
  }
  if(job.results.length) {
    const link=document.createElement("a");link.className="secondary";
    link.href=`#/jobs/${job.id}/results/${encodeURIComponent(job.results[0].id)}`;
    link.textContent=`查看已完成结构（${job.results.length}）→`;
    $("job-detail").querySelector(".card-heading .button-row").append(link);
  }
  if(!job.unavailable&&["succeeded","failed","cancelled"].includes(job.state)){
    const archiveButton=document.createElement("button");archiveButton.className="secondary";
    archiveButton.dataset.action=job.archived?"unarchive":"archive";
    archiveButton.textContent=job.archived?"取消归档":"归档";
    $("job-detail").querySelector(".card-heading .button-row").append(archiveButton);
  }
  $("job-detail").querySelectorAll("[data-action]").forEach(button=>button.addEventListener("click",()=>busy(button,async()=>{
    try {
      const action=button.dataset.action;
      if(action==="cancel"&&!confirm("取消该项目的作业并停止后续调度？")) return;
      if(action==="retry"&&!confirm("沿用原输入和参数，重试失败或取消的阶段？已完成结果将保留。")) return;
      await api(`jobs/${job.id}/${action}`,{});
      detailSignature="";await openJob();
    } catch(error) {notify(error.message,true);}
  })));
}
async function loadExtras(kind,offset=0) {
  const jobId=currentJob,version=++extraVersions[kind];
  const target=$(kind+"-content");
  target.textContent="正在加载…";
  const current=()=>jobId===currentJob&&currentPage==="project"&&version===extraVersions[kind]&&$("task-"+kind).open;
  try {
    const data=await api(`jobs/${jobId}/extras?kind=${kind}&offset=${offset}&limit=${detailPageSize}`);
    if(!current())return;
    if(kind==="logs") {
      target.innerHTML=`<h3>控制器</h3><pre class="task-log">${esc(data.controller_log||"暂无日志")}</pre>`+
        data.items.map(t=>`<details><summary>${esc(t.id)} · ${esc(t.job_id)}</summary><pre class="task-log">${esc(t.log_tail)}</pre></details>`).join("");
    } else {
      const routing=data.gpu_routing;
      target.innerHTML=(routing?`<p>GPU 自动调度 · ${date(new Date(routing.checked_at*1000).toISOString())}</p>`+
        routing.targets.map(t=>`<p><b>${esc(gpuResourceLabel(t))}</b> · 空闲 GPU ${esc(t.free_gpus)}${t.error?` · ${esc(t.error)}`:""}</p>`).join(""):"")+
        data.items.map(t=>`<details class="help"><summary>${esc(t.id)} · ${esc(t.routing_reason||"转投记录")}</summary><p>${t.route?`输入传输 ${Number(t.route.input_bytes/1024).toFixed(1)} KiB · SHA-256 ${esc(t.route.input_sha256)}<br>执行 ${esc(t.host)} / ${esc(t.route.target?.backend==="baremetal"?"裸金属":t.route.resources?.partition)} · 结果${t.imported?"已校验回传":"等待回传"}`:""}</p>${(t.history||[]).map(h=>`<p>attempt ${esc(h.attempts)} · ${esc(h.host)} · job ${esc(h.job_id)} · ${esc(h.execution_state||h.slurm_state)}</p>`).join("")}</details>`).join("");
    }
    target.innerHTML+=pager(kind,offset,data.total);
    target.querySelectorAll("[data-page]").forEach(button=>button.addEventListener("click",()=>loadExtras(kind,Number(button.dataset.offset))));
  } catch(error) {if(current())target.textContent="加载失败："+error.message+"；请收起后重新展开。";}
}
async function refreshHealth() {
  try {
    const health=await api("health");
    $("connection").textContent="本地服务已连接";
    $("manager-health").textContent=!health.enabled?"只读模式，调度已禁用":!health.alive?"后台服务已停止，请检查服务日志":Object.keys(health.errors).length?`后台检查异常：${Object.entries(health.errors).map(([id,error])=>`${id}: ${error}`).join("；")}`:"后台服务正常";
  } catch(error) {$("connection").textContent="连接失败";$("manager-health").textContent="后台状态读取失败："+error.message;}
}
async function openResultPage(job,resultId) {
  currentResult=job.results.findIndex(r=>String(r.id)===String(resultId));
  if(currentResult<0)throw new Error("该预测尚未就绪或已不可用，请返回项目详情刷新。");
  $("result-back").href=`#/jobs/${job.id}`;
  $("results-panel").innerHTML=`<div class="card-heading"><h2>${esc(job.name)} · 结构与指标</h2><select id="result-select" aria-label="选择预测结果" style="max-width:420px">${job.results.map((r,i)=>`<option value="${i}">${esc(r.name)} · seed ${esc(r.seed)} · sample ${esc(r.sample)}</option>`).join("")}</select></div><div id="result-content"></div>`;
  $("result-select").value=String(currentResult);
  $("result-select").addEventListener("change",()=>{window.location.hash=`#/jobs/${job.id}/results/${encodeURIComponent(job.results[Number($("result-select").value)].id)}`;});
  await showResult(job);
}

function confidenceColor(value) {
  return value==null?"#c4ccc8":value>=90?"#1554cb":value>=70?"#65c4ef":value>=50?"#f3d34f":"#ef8b38";
}
function readableText(background) {
  const rgb=background.replace("#","").match(/../g).map(v=>parseInt(v,16)/255);
  const linear=rgb.map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);
  const luminance=linear[0]*0.2126+linear[1]*0.7152+linear[2]*0.0722;
  return (luminance+0.05)/0.05>=1.05/(luminance+0.05)?"#000000":"#ffffff";
}
function designColor(designed) {return designed===true?"#c45224":designed===false?"#397a91":"#c4ccc8";}
function num(value) {return typeof value==="number"?value.toFixed(3).replace(/\.?0+$/,""):value??"—";}
function resultFasta(result) {
  const chains=new Map(), records=[];
  const bases={A:"A",C:"C",G:"G",U:"U",I:"I",N:"N",DA:"A",DC:"C",DG:"G",DT:"T",DU:"U",DI:"I",DN:"N"};
  for(const row of result.residues) {
    if(!chains.has(row.chain))chains.set(row.chain,[]);
    chains.get(row.chain).push(row);
  }
  const token=value=>String(value??"").replace(/\s+/g,"_").replace(/[>|]/g,"_");
  for(const [chain,rows] of chains) {
    const protein=rows.some(r=>Object.hasOwn(aminoAcids,r.resname)||r.resname==="UNK"||["N","CA","C"].every(a=>r.atoms?.includes(a)));
    const nucleic=rows.some(r=>["C1'","C3'","C4'"].every(a=>r.atoms?.includes(a)));
    if(!protein&&!nucleic)continue;
    const alphabet=protein?aminoAcids:bases,unknown=protein?"X":"N";
    const sequence=rows.map(r=>Object.hasOwn(alphabet,r.resname)?alphabet[r.resname]:unknown).join("");
    const identity=[`>${token(result.name||result.id)}|chain=${token(chain)||"_"}`];
    if(result.seed!=null)identity.push(`seed=${token(result.seed)}`);
    if(result.sample!=null)identity.push(`sample=${token(result.sample)}`);
    records.push(`${identity.join(" ")}\n${sequence.match(/.{1,80}/g).join("\n")}`);
  }
  return records.length?records.join("\n")+"\n":"";
}
function structureDownloadName(job,result) {
  const project=String(job.name||job.id).replace(/[<>:"/\\|?*\u0000-\u001f]/g,"_").trim().slice(0,100).replace(/[ .]+$/,"")||"project";
  const identity=String(result.metrics?.design??result.id).match(/^(?:metrics_)?(?:b(\d+)_s(\d+)|input_(\d+))(?:_seed|$)/);
  // Standalone folding uses backbone slot 0 and its input record's sequence index.
  const backbone=identity?.[1]??(identity?.[3]!==undefined?"0":null),sequence=identity?.[2]??identity?.[3];
  const number=(value,width)=>value!=null&&/^\d+$/.test(String(value))?String(value).replace(/^0+(?=\d)/,"").padStart(width,"0"):"unknown";
  return `${project}-b${number(backbone,3)}-s${number(sequence,4)}-seed${number(result.seed??result.metrics?.fold_seed,3)}-sample${number(result.sample??result.metrics?.sample,3)}.cif`;
}
async function showResult(job) {
  const version=++resultVersion, index=currentResult;
  const current=()=>version===resultVersion&&job.id===currentJob&&index===currentResult&&currentPage==="result";
  $("result-content").textContent="正在载入所选结果…";
  const result=await api(`jobs/${job.id}/results/${job.results[index].id}`), conf=result.confidence;
  if(!current())return;
  const resultURL=`/api/jobs/${encodeURIComponent(job.id)}/results/${encodeURIComponent(result.id)}`;
  const response=await fetch(`${resultURL}/structure`);
  if(!response.ok)throw new Error(`结构读取失败：HTTP ${response.status}`);
  const text=await response.text();
  if(!current())return;
  const metricKeys=["plddt","ptm","iptm","ranking_score","gpde","has_clash"].filter(k=>conf[k]!==undefined);
  const rmsd=Object.entries(result.metrics).filter(([k,v])=>/rmsd|coverage|identity/.test(k)&&typeof v==="number");
  $("result-content").innerHTML=`<div class="metric-grid">${metricKeys.map(k=>`<div class="metric"><small>${esc(k)}</small><b>${esc(num(conf[k]))}</b></div>`).join("")}</div><label for="result-color-mode">结构与序列着色</label><select id="result-color-mode"><option value="confidence">置信度 pLDDT</option><option value="design" ${result.design_mapping_available?"":"disabled"}>设计区域 / 保留区域</option></select>${result.design_mapping_available?'':'<p class="small-note">无设计映射，仅显示 pLDDT 着色。</p>'}<div class="results-grid"><div>
    <div class="button-row result-actions" aria-label="当前结构操作">
      <button id="copy-result-sequence" type="button" class="secondary" title="复制当前结构已解析的分链 FASTA 序列；未知蛋白/核酸残基用 X/N，不含配体和离子。">复制序列</button>
      <button id="reset-result-view" type="button" class="secondary" disabled>重置视图</button>
      <a id="download-result-cif" class="secondary" href="${esc(resultURL)}/structure" download="${esc(structureDownloadName(job,result))}">下载当前 CIF ↓</a>
    </div>
    <p id="result-action-status" class="small-note result-action-status" role="status" aria-live="polite"></p>
    <div id="result-sequence-fallback" hidden><label for="result-sequence-text">FASTA 序列（请手动复制）</label><textarea id="result-sequence-text" class="code-input" rows="5" readonly spellcheck="false"></textarea></div>
    <div class="result-view" id="result-viewer"></div>
    <div id="result-color-legend" class="plddt-legend"></div>
    <div class="button-row"><a class="text-button" href="${esc(resultURL)}/confidence" download>置信度摘要 JSON ↓</a>${result.full_confidence_file?`<a class="text-button" href="${esc(resultURL)}/full-confidence" download>完整置信度 JSON ↓</a>`:""}</div>
    <div id="confidence-chart"></div><div id="result-residues" class="result-residues"></div><div id="residue-info" class="small-note">点击残基查看信息并定位结构。</div></div>
    <div><h3>PAE · 预测对齐误差</h3>${result.pae?'<canvas id="pae" class="pae-canvas"></canvas><p id="pae-value" class="small-note">悬停查看 token 配对的 PAE（Å）</p>':'<div class="empty-state"><p>未提供可解析的 PAE 矩阵。</p></div>'}
    <h3>结构指标</h3><table>${rmsd.map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(num(v))}</td></tr>`).join("")||'<tr><td class="muted">独立 Folding 无参考结构，不计算 RMSD。</td></tr>'}</table><p class="small-note">计算指标不代表实验功能。</p></div></div>`;
  const copyButton=$("copy-result-sequence"),resetButton=$("reset-result-view"),actionStatus=$("result-action-status"),fasta=resultFasta(result);
  copyButton.disabled=!fasta;
  if(!fasta)actionStatus.textContent="没有可复制的蛋白质或核酸序列。";
  copyButton.addEventListener("click",async()=>{
    if(!current()||!fasta)return;
    copyButton.disabled=true;
    $("result-sequence-fallback").hidden=true;
    try {
      await navigator.clipboard.writeText(fasta);
      if(current())actionStatus.textContent="已复制分链 FASTA 序列。";
    } catch(error) {
      if(current()) {
        actionStatus.textContent="自动复制失败，请在下方手动复制。";
        $("result-sequence-fallback").hidden=false;
        const field=$("result-sequence-text");field.value=fasta;field.focus();field.select();
      }
    } finally {copyButton.disabled=false;}
  });
  resultViewer=null;
  try {
    resultViewer=$3Dmol.createViewer($("result-viewer"),{backgroundColor:"#f1f5f1",antialias:true});
    resultViewer.addModel(text,"cif");
    resultViewer.setStyle({},{cartoon:{colorfunc:a=>confidenceColor(a.b)}});
    resultViewer.zoomTo();resultViewer.render();
    const viewer=resultViewer,initialView=viewer.getView().slice();
    resetButton.disabled=false;
    resetButton.addEventListener("click",()=>{
      if(!current())return;
      viewer.zoomTo(); // Restore the full-model clipping planes after residue focus.
      viewer.setView(initialView);viewer.render();
      actionStatus.textContent="视图已重置。";
    });
  } catch(error){resultViewer=null;$("result-viewer").textContent="WebGL 不可用，可下载 CIF 查看。";}
  const rows=result.residues,valid=rows.filter(r=>r.plddt!=null);
  if(valid.length){
    const points=rows.map((r,i)=>`${rows.length>1?i/(rows.length-1)*700:0},${90-(r.plddt??0)*.8}`).join(" ");
    $("confidence-chart").innerHTML=`<svg class="confidence-chart" viewBox="0 0 700 100" preserveAspectRatio="none" aria-label="逐残基 pLDDT 曲线"><path d="M0 50 H700" stroke="#dbe5d8" stroke-dasharray="4"/><polyline points="${points}" fill="none" stroke="#2d8d7c" stroke-width="1.5"/></svg>`;
  }
  const designation=r=>r.is_designed===true?"设计区域":r.is_designed===false?"保留区域":"设计映射不可用";
  $("result-residues").innerHTML=rows.map((r,i)=>`<button data-residue="${i}" aria-label="${esc(r.chain)}:${esc(r.position)} ${esc(r.resname)} · ${designation(r)} · pLDDT ${esc(num(r.plddt))}" title="链 ${esc(r.chain)} · 编号 ${esc(r.position)} · ${esc(r.resname)} (${aminoLetter(r.resname)}) · ${designation(r)} · pLDDT ${esc(num(r.plddt))}">${aminoLetter(r.resname)}</button>`).join("");
  const mapped=new Map(rows.map(r=>[`${r.chain}:${r.position}`,r.is_designed]));
  const paint=()=>{
    const mode=$("result-color-mode").value;
    resultColorMode=mode;
    if(resultViewer){resultViewer.setStyle({},{cartoon:{colorfunc:a=>mode==="design"?designColor(mapped.get(`${a.chain}:${a.resi}`)):confidenceColor(a.b)}});resultViewer.render();}
    $("result-residues").querySelectorAll("button").forEach(button=>{
      const r=rows[Number(button.dataset.residue)],color=mode==="design"?designColor(r.is_designed):confidenceColor(r.plddt);
      button.style.background=color;button.style.color=readableText(color);
    });
    $("result-color-legend").innerHTML=mode==="design"?'<span><i style="background:#c45224"></i>设计区域</span><span><i style="background:#397a91"></i>保留区域</span><span>设计位点不一定发生突变</span>':`${[[90,"≥90"],[70,"70–90"],[50,"50–70"],[0,"<50"]].map(([v,label])=>`<span><i style="background:${confidenceColor(v)}"></i>${label}</span>`).join("")}<span>pLDDT</span>`;
  };
  $("result-color-mode").value=result.design_mapping_available?resultColorMode:"confidence";
  $("result-color-mode").addEventListener("change",paint);paint();
  $("result-residues").querySelectorAll("button").forEach(button=>button.addEventListener("click",()=>{
    const r=rows[Number(button.dataset.residue)];
    $("residue-info").textContent=`${r.chain}:${r.position} ${r.resname} · ${designation(r)} · pLDDT ${num(r.plddt)}（原子平均值）`;
    // A pending animated zoom would continue changing the camera after reset.
    resultViewer?.zoomTo({chain:r.chain,resi:Number(r.position)});resultViewer?.render();
  }));
  if(result.pae){
    const canvas=$("pae"),n=result.pae.length;canvas.width=canvas.height=n;
    const ctx=canvas.getContext("2d"),image=ctx.createImageData(n,n);
    for(let y=0;y<n;y++)for(let x=0;x<n;x++){
      const t=Math.min(1,result.pae[y][x]/31.75),offset=4*(y*n+x);
      image.data.set([Math.round(29+219*t),Math.round(92+157*t),Math.round(64+176*t),255],offset);
    }
    ctx.putImageData(image,0,0);
    canvas.addEventListener("mousemove",event=>{
      const rect=canvas.getBoundingClientRect(),x=Math.min(n-1,Math.floor((event.clientX-rect.left)/rect.width*n)),y=Math.min(n-1,Math.floor((event.clientY-rect.top)/rect.height*n));
      $("pae-value").textContent=`${result.pae_labels?.[y]??`token ${y+1}`} → ${result.pae_labels?.[x]??`token ${x+1}`}：${result.pae[y][x].toFixed(2)} Å`;
    });
  }
}
window.addEventListener("resize",()=>{designViewer?.resize();resultViewer?.resize();});
async function start() {
  // Route content does not wait for resource settings or the health endpoint.
  const route=loadRoute().catch(error=>notify(error.message,true));
  const settings=Promise.all([api("config"),api("resources"),api("scheduler-limits")]);
  refreshHealth();
  const [configuration,resourceSettings,limits]=await settings;
  config=configuration;globalResources=resourceSettings;
  applyConfiguredDefaults(config.defaults||{});
  renderResourceSettings();
  on("save-resources","click",()=>busy($("save-resources"),async()=>{
    const settings={version:1,gpu_targets:[...document.querySelectorAll('[data-resource="gpu"]:checked')].map(el=>el.value),msa_backends:[...document.querySelectorAll('[data-resource="msa"]:checked')].map(el=>el.value),queue_grace_seconds:Number($("resource-queue-grace").value)};
    globalResources=await api("resources",settings);
    renderResourceSettings();
    $("resources-status").textContent="已保存，后续调度生效。";
  }));
  $("gpu-limit").value=limits.max_gpu_jobs;$("msa-limit").value=limits.max_msa_jobs;
  on("save-limits","click",()=>busy($("save-limits"),async()=>{
    const gpu=Number($("gpu-limit").value),msa=Number($("msa-limit").value);
    if(!Number.isSafeInteger(gpu)||gpu<0||!Number.isSafeInteger(msa)||msa<1)throw new Error("GPU 上限需为非负整数，MSA 上限需为正整数");
    const saved=await api("scheduler-limits",{max_gpu_jobs:gpu,max_msa_jobs:msa});
    $("limits-status").textContent=`已保存：GPU ${saved.max_gpu_jobs||"不限"}，MSA ${saved.max_msa_jobs}。后续调度生效，已有作业继续运行。`;
  }));
  $("filter-profile").innerHTML+='<option value="auto">全局自动调度</option>';
  $("filter-profile").innerHTML+=[...config.profiles].map(p=>`<option value="${esc(p.id)}">${esc(p.id)}</option>`).join("");
  resources("fold");resources("design");
  if(!config.profiles.length)notify("尚未配置计算后端。请在 configs/webapp.yaml 设置 server_profile 后重启。",true);
  await route;
  setInterval(()=>{
    if(document.hidden)return;
    if(currentPage==="monitor"){refreshJobs();refreshHealth();}
    else if(currentPage==="project")openJob().catch(e=>notify(e.message,true));
  },10000);
}
start().catch(error=>notify(error.message,true));
