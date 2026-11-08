import React,{useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Upload,ArrowRight,ArrowLeft,Check,Scissors,ChevronDown,FileText,FolderOpen,Download,X,Loader2,Info,AlertTriangle,Plus,HelpCircle} from 'lucide-react';
import '@fontsource-variable/dm-sans';
import '@fontsource-variable/manrope';
import {Sources,Quality,Exports,Shortlist} from './advanced';
import './style.css';
import './simple.css';

async function api(path,options={}){
 const response=await fetch('/api'+path,{...options,headers:{'X-Promo-Client':'studio',...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...options.headers}});
 const result=await response.json();
 if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Please check the entered values and try again.');
 return result;
}
const time=(ms=0)=>`${String(Math.floor(ms/60000)).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;
const clock=value=>value.includes(':')?Math.round(value.split(':').map(Number).reduce((n,v)=>n*60+v,0)*1000):Math.round(Number(value)*1000);
const plural=(n,word)=>`${n} ${word}${n===1?'':'s'}`;
function Button({children,secondary=false,...props}){return <button className={`s-button ${secondary?'secondary':''}`} {...props}>{children}</button>}
function App(){
 const [projects,setProjects]=useState([]),[project,setProject]=useState(null),[server,setServer]=useState(null);
 const [step,setStep]=useState(1),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const [rows,setRows]=useState([]),[cuts,setCuts]=useState([]),[loadingRows,setLoadingRows]=useState(false),[chosen,setChosen]=useState([]);
 const [savedOpen,setSavedOpen]=useState(false),[advanced,setAdvanced]=useState(null),[help,setHelp]=useState(false);
 const [profile,setProfile]=useState('groq'),[localModel,setLocalModel]=useState('llama3.1:8b');
 const [plan,setPlan]=useState(null);
 const [reviewTier,setReviewTier]=useState('promo_cuts'),[tierLimit,setTierLimit]=useState(12);
 const [visitorKey,setVisitorKey]=useState('');
 const pid=project?.id;
 useEffect(()=>{
  setPlan(null);if(!pid||!chosen.length||project?.batch)return;
  let alive=true;const timer=setTimeout(()=>api(`/projects/${pid}/plan`,{method:'POST',body:JSON.stringify({episode_numbers:chosen,profile,local_model:localModel})}).then(p=>alive&&setPlan(p)).catch(()=>{}),300);
  return()=>{alive=false;clearTimeout(timer)};
 },[pid,chosen,profile,localModel,!!project?.batch]);
 async function listProjects(){const p=await api('/projects');setProjects(p);return p;}
 async function openProject(id,preferred){
  setError('');setBusy(true);
  try{
   const [p,saved]=await Promise.all([api('/projects/'+id),api(`/projects/${id}/promo-cuts`)]);setProject(p);setChosen(p.selected_episodes);setRows([]);setCuts(saved);setReviewTier('promo_cuts');setProfile(p.processing_profile||'groq');if(p.local_model)setLocalModel(p.local_model);
   localStorage.setItem('promo-project',p.id);setSavedOpen(false);
   setStep(preferred??(saved.length||p.batch?.status==='complete_subset'?3:2));
  }catch(e){setError(e.message)}finally{setBusy(false)}
 }
 useEffect(()=>{
  let alive=true;
  Promise.all([api('/projects'),api('/status')]).then(async([ps,status])=>{
   if(!alive)return;setProjects(ps);setServer(status);
   const saved=localStorage.getItem('promo-project');
   if(saved&&!ps.some(p=>p.id===saved))localStorage.removeItem('promo-project');
   if(saved&&ps.some(p=>p.id===saved)){
    const p=await api('/projects/'+saved);
    if(alive){setProject(p);setChosen(p.selected_episodes);setProfile(p.processing_profile||'groq');if(p.local_model)setLocalModel(p.local_model)}
   }
  }).catch(e=>alive&&setError(e.message));
  return()=>{alive=false};
 },[]);
 useEffect(()=>{
  if(!pid||step!==3&&!advanced)return;
  let alive=true;setLoadingRows(true);
  api(`/projects/${pid}/promo-cuts`).then(c=>alive&&setCuts(c)).catch(e=>alive&&setError(e.message)).finally(()=>alive&&setLoadingRows(false));
  api(`/projects/${pid}/candidates`).then(r=>alive&&setRows(r)).catch(e=>alive&&setError(e.message));
  return()=>{alive=false};
 },[pid,step,advanced,project?.job?.status,project?.batch?.status,project?.batch?.blind_reviewed,project?.batch?.context_reviewed]);
 useEffect(()=>{
  if(!pid||project?.job?.status!=='running')return;
  let active=true;
  const timer=setInterval(async()=>{
   try{
    const p=await api('/projects/'+pid);if(!active)return;setProject(current=>current?.id===p.id?p:current);
    if(p.job?.status==='complete_subset')setStep(current=>current===2?3:current);
   }catch(e){setError(e.message)}
  },2500);
  return()=>{active=false;clearInterval(timer)};
 },[pid,project?.job?.status]);
 async function imported(p){setProject(p);setChosen(p.selected_episodes);setProfile(p.processing_profile||'groq');setRows([]);setCuts([]);setReviewTier('promo_cuts');setError('');localStorage.setItem('promo-project',p.id);const saved=await api(`/projects/${p.id}/promo-cuts`);setCuts(saved);setStep(saved.length?3:2);await listProjects();}
 async function freshRun(){setBusy(true);setError('');try{await imported(await api(`/projects/${pid}/new-run`,{method:'POST'}));setNotice('New run ready. All usable episodes are selected. Your previous run is still in Saved work.')}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function analyze(){
  setBusy(true);setError('');
  try{
   await api(`/projects/${pid}/jobs`,{method:'POST',body:JSON.stringify({episode_numbers:chosen,profile,local_model:localModel})});
   setProject(await api('/projects/'+pid));
  }catch(e){setError(e.message)}finally{setBusy(false)}
 }
 async function connectVisitorKey(e){e.preventDefault();setBusy(true);setError('');try{await api('/session/key',{method:'POST',body:JSON.stringify({api_key:visitorKey})});setVisitorKey('');setServer(await api('/status'));setNotice('Your Groq key is connected for this session. It is not saved in browser storage or project files.')}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function clearPublicSession(){if(!window.confirm('Download your outputs first. Clear this session’s uploaded scripts and results? Usage accounting will remain.'))return;setBusy(true);try{await api('/session/clear',{method:'POST'});localStorage.removeItem('promo-project');setProject(null);setProjects([]);setRows([]);setCuts([]);setStep(1);setServer(await api('/status'));setNotice('This session is empty. Your previous downloads are unchanged.')}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function reloadRows(){const [r,c]=await Promise.all([api(`/projects/${pid}/candidates`),api(`/projects/${pid}/promo-cuts`)]);setRows(r);setCuts(c)}
 const running=project?.job?.status==='running';
 const suggested=rows.filter(r=>r.status==='ai_eligible');
 const judgment=rows.filter(r=>r.status==='editor_review');
 const unreviewed=rows.filter(r=>!['ai_eligible','editor_review','rejected','blind_rejected','invalid'].includes(r.status));
 const tierRows=reviewTier==='promo_cuts'?cuts:reviewTier==='ai_suggestions'?suggested:reviewTier==='needs_judgment'?judgment:unreviewed;
 const kept=[...new Map([...rows,...cuts].filter(r=>r.editor_note?.verdict==='accept').map(r=>[r.candidate_id,r])).values()];
 const hasRun=!!project?.batch;
 const available=project?.episodes.filter(e=>e.source_ready&&!e.reference_reserved)||[];
 const connected=profile==='local'?(server?.local_models||[]).includes(localModel):profile==='hybrid'?server?.api_configured&&(server?.local_models||[]).includes(localModel):server?.api_configured;
 function move(n){setStep(n);setError('');setNotice('');}
 return <div className="simple-app">
  <header className="simple-header">
   <a href="#" className="simple-brand" onClick={e=>{e.preventDefault();move(1)}}><span><Scissors size={21}/></span><b>KUKU <small>Promo Finder</small></b></a>
   <div className="simple-header-actions">{projects.length>0&&<button onClick={()=>{listProjects().catch(e=>setError(e.message));setSavedOpen(true)}}><FolderOpen size={16}/><span>Saved work</span></button>}<button aria-label="How to use this app" onClick={()=>setHelp(true)}><HelpCircle size={18}/></button></div>
  </header>
  <main className="simple-main">
   <nav className="simple-steps" aria-label="Your three steps">
    {['Add scripts','Find promos','Review & download'].map((label,index)=><button key={label} aria-current={step===index+1?'step':undefined} className={step===index+1?'current':step>index+1?'complete':''} disabled={index>0&&!project} onClick={()=>move(index+1)}><span>{step>index+1?<Check size={13}/>:index+1}</span><b>{label}</b></button>)}
   </nav>
   {error&&<div className="s-alert error" role="alert"><AlertTriangle size={18}/><div>{error}</div><button onClick={()=>setError('')} aria-label="Dismiss error"><X size={15}/></button></div>}
   {notice&&<div className="s-alert success" role="status"><Check size={17}/>{notice}</div>}
   {server?.mode==='public_session'&&<section className="public-session"><strong>Public prototype · your own session</strong><p>Upload your timed scripts, process them and download promo cut instructions. No account registration is required. Processing uses the configured Groq account and its free usage limits. Uploads and results stay separate from other visitors; download before the free server restarts.</p><Button secondary disabled={busy||running} onClick={clearPublicSession}>Clear this session<X size={15}/></Button><details className="s-details" open={!server.api_configured}><summary>{server.shared_key_configured?'Shared Groq account connected · optional visitor key':server.api_configured?'Groq connected · change key':'Connect your Groq key to process new uploads'}</summary><form onSubmit={connectVisitorKey}><label className="s-field">Your Groq API key<input type="password" autoComplete="off" spellCheck={false} value={visitorKey} onChange={e=>setVisitorKey(e.target.value)} placeholder="gsk_…" required/></label><p className="s-note">Your key is sent over HTTPS to this app and used with Groq. It stays in server memory for this browser session, never in downloads or browser storage. Your selected script text goes to Groq when you start processing. No company screenplay or previous visitor result is preloaded.</p><Button disabled={busy||!visitorKey}>Connect key</Button></form></details></section>}
   {step===1&&<>
    <div className="simple-intro"><p className="s-kicker">STEP 1 OF 3</p><h1>Turn your scripts into<br/>promo suggestions.</h1><p>Upload the episodes. We’ll find 20–60 second clips with a strong opening and an ending that makes viewers want more.</p></div>
    <UploadStep onImported={imported} onError={setError}/>

    {project&&<div className="continue-work"><FolderOpen size={20}/><div><strong>{project.name}</strong><span>Your previous work is saved.</span></div><button onClick={()=>move(hasRun?3:2)}>Continue<ArrowRight size={16}/></button></div>}
   </>}
   {step===2&&project&&<>
    <div className="simple-intro"><p className="s-kicker">STEP 2 OF 3</p><h1>{running?'Finding your promos…':'Your scripts are loaded.'}</h1><p>{running?'We’re reading the selected episodes and checking which clips work on their own. You can leave this page; progress is saved.':'We’ll look for an opening hook, a clear story and an unresolved ending.'}</p></div>
    <section className="simple-panel run-choice">
     <div className="show-name"><FileText size={21}/><div><strong>{project.name}</strong><span>{plural(project.episodes.length,'episode')} found in your files</span></div></div>
     <ScriptIssues project={project}/>
     {project.saved_read_only||project.batch?.status==='complete_subset'?<div className="saved-result"><Check size={20}/><h2>There are saved results for this show.</h2><p>{plural(project.batch?.ai_analyzed_episodes||0,'episode')} analyzed. Open the suggestions to read the cuts and decide what to keep.</p><Button onClick={()=>move(3)}>See my promos<ArrowRight size={17}/></Button>{project.batch?.coverage_pass&&!project.batch?.whole_show_results&&!project.saved_read_only&&<><p>Coverage is incomplete. You can check available capacity and continue the same saved run.</p><Button secondary disabled={busy||running} onClick={analyze}>Check capacity & continue<ArrowRight size={16}/></Button></>}</div>:<>
      <div className="selection-summary"><strong>{plural(chosen.length,'episode')} selected</strong><span>Only these episodes will use AI analysis.</span></div>
      <div className="episode-scope" role="group" aria-label="Episode selection"><button disabled={hasRun||running} aria-pressed={chosen.length===available.length} onClick={()=>setChosen(available.map(e=>e.number))}>Analyze all {available.length} usable episodes</button><button disabled={hasRun||running} onClick={()=>{setChosen([]);document.getElementById('episode-picker').open=true}}>Choose episodes myself</button></div>
      <p className="s-note">{chosen.length===available.length?'Whole-show search: every usable episode will be read, then promising clips will be compared across the show.':'Selected-episode search: results cover this selection, so they cannot establish the best promos across the whole show.'}</p>
      <details id="episode-picker" className="s-details"><summary>See or change selected episodes</summary><div className="episode-choices">{project.episodes.map(e=><label key={e.id}><input type="checkbox" checked={chosen.includes(e.number)} disabled={!!project.batch||running||!e.source_ready||e.reference_reserved} onChange={()=>setChosen(values=>values.includes(e.number)?values.filter(n=>n!==e.number):[...values,e.number])}/><span><strong>Episode {e.number}</strong><small>{e.reference_reserved?'Set aside for a quality test':!e.source_ready?'Timing needs fixing':e.filename}</small></span></label>)}</div></details>
      {!hasRun&&<div className="processing-choice"><label className="s-field">Where should analysis run?<select aria-label="Processing mode" value={profile} onChange={e=>setProfile(e.target.value)}><option value="groq">Groq · faster model responses, free usage limits</option><option value="hybrid" disabled={!server?.local_models?.length||!server?.api_configured}>Hybrid · local story analysis, Groq clip reviews</option><option value="local" disabled={!server?.local_models?.length}>On this Mac · no API key or daily quota</option></select></label>{profile!=='groq'?<><label className="s-field">Installed local model<select value={localModel} onChange={e=>setLocalModel(e.target.value)}>{(server?.local_models||[]).map(m=><option key={m} value={m}>{m}</option>)}</select></label><p className="s-note">{profile==='hybrid'?'Episode analysis stays local. Selected clips and related source evidence go to Groq for audience and context reviews. This reduces cloud usage; local analysis quality still needs your inspection.':'Your scripts stay on this Mac. Processing can be slower, and all reviews use the same model. Treat the results as exploratory until you inspect them.'}</p></>:<p className="s-note">Scripts go to your configured Groq account. The app saves progress and pauses safely if capacity runs out.</p>}<p className="s-fineprint">Every selected episode is analysed. Audience reviews cover different episodes before second cuts, up to two per episode within the usage budget. Up to 30 clips receive source checks; up to 20 passing suggestions are ranked. Unreviewed clips are not rejected.</p></div>}
      {running?<RunProgress project={project}/>:<>
       {plan?.analysis_estimate_fits===false&&<div className="s-alert warning"><Info size={17}/><span>This selection is likely to pause at the Groq analysis usage limit. Hybrid mode moves episode analysis onto this Mac; local mode removes API quota waits. Neither guarantees faster processing or equal quality.</span></div>}
       {project.job&&<div className="s-alert warning"><Info size={17}/><span>{project.job.status==='paused_budget'||project.job.status==='paused'?'The free usage limit paused this run. Your completed work is saved. Continue when capacity is available.':project.job.message}</span></div>}
       <Button disabled={busy||!chosen.length||!connected} onClick={analyze}>{busy?<Loader2 className="spin" size={17}/>:<Scissors size={17}/>} {project.batch?'Continue this saved run':`Find promos in ${plural(chosen.length,'episode')}`}</Button>
       <p className="s-fineprint">Analysis starts only when you click. Completed work is saved for resume.</p>
       {!connected&&<div className="s-alert warning">{server?.mode==='public_session'?'Connect your Groq key above to process your upload. No login is needed.':profile==='local'?'The selected local model is unavailable. Choose an installed model.':'Add your Groq key to the backend .env, or choose an available local model.'}</div>}
      </>}
     </>}
     {hasRun&&!running&&!project.saved_read_only&&<div className="new-run-choice"><p>Want all episodes, a different selection or the updated pipeline? Start a separate run from these same uploaded scripts.</p><Button secondary disabled={busy} onClick={freshRun}>Start a new run<Plus size={16}/></Button><small>Your existing results and original model settings are preserved.</small></div>}
     <details className="s-details"><summary>Read the uploaded scripts</summary><SourceReader pid={pid} episodes={project.episodes}/></details>
    </section>
   </>}
   {step===3&&project&&<>
    <div className="simple-intro review-intro"><p className="s-kicker">STEP 3 OF 3</p><h1>{loadingRows?'Loading your cuts…':'Your promo cuts.'}</h1><p>Get the episode, exact start and end, opening hook and ending for each cut. Download the cut sheet with the selected screenplay for your editor.</p><p className="s-note">Script-based cut instructions. The assignment does not require an edited video; footage is not part of this upload.</p></div>
    <div className="review-context"><span>{project.origin==='demo_saved'?'Example show':project.name}</span><small>{cuts.some(c=>c.reused_saved_cut)?'Exact-source saved cuts reused · no new AI calls for these cuts':`${plural(project.batch?.ai_analyzed_episodes||0,'episode')} analyzed${project.batch?.ai_analyzed_episodes<project.episodes.length?` out of ${project.episodes.length}`:''}`}</small></div>
    {project.batch?.coverage_pass&&<div className="coverage-note" role="status"><strong>{project.batch.coverage_pass.reviewed_episodes} of {project.batch.coverage_pass.episodes_with_candidates} episode-first candidates reviewed</strong><p>{project.batch.coverage_pass.eligible_episodes} episodes eligible for this coverage pass. {project.batch.coverage_pass.excluded_episode_numbers.length} reserved or exposed episodes kept separate; {project.batch.coverage_pass.source_exclusions.length} excluded for source timing. {project.batch.whole_show_results?'The eligible coverage pass is complete.':'Coverage is limited; this is not a complete whole-show recommendation list.'}</p></div>}
    {project.batch?.provisional&&<p className="example-note"><Info size={15}/>Limited or provisional results: only part of the eligible show may have completed review. These clips do not establish the final whole-show ranking.</p>}{running&&<p className="example-note"><Loader2 className="spin" size={15}/>Analysis is still running. More suggestions may appear.</p>}{project.origin==='demo_saved'&&<p className="example-note"><Info size={15}/>These are real saved AI suggestions for invented scripts. You can explore without spending quota.</p>}
    {!!cuts.length&&<section className="simple-panel cut-delivery"><div><Scissors size={23}/><h2>{plural(cuts.length,'promo cut')} available</h2><p>{cuts.filter(c=>c.delivery_kind==='draft_editor_check').length} are draft cuts with editor warnings. Drafts keep their original AI status; they are not automatically approved.</p>{cuts.some(c=>c.reused_saved_cut)&&<p>These cuts were reused from a saved run for the exact same screenplay files. This upload did not make new AI calls for them.</p>}</div><div className="cut-downloads"><a className="s-button" href={`/api/projects/${pid}/export/promo-cuts-txt`}><Download size={16}/>Download cuts & script</a><a className="s-button secondary" href={`/api/projects/${pid}/export/promo-cuts-csv`}>Download timestamps CSV</a></div></section>}
    {!hasRun&&!loadingRows&&!cuts.length&&<div className="simple-panel s-empty"><Scissors size={30}/><h2>Your scripts are ready to process.</h2><p>We need to analyse this new screenplay before we can give you source-checked promo cuts. No result has been invented.</p><Button onClick={()=>move(2)}>Find my promo cuts<ArrowRight size={16}/></Button></div>}
    {loadingRows&&<div className="s-loading"><Loader2 className="spin" size={23}/>Loading saved clips…</div>}
    {(hasRun||cuts.length>0)&&!loadingRows&&<><div className="review-tiers" role="group" aria-label="Cut results"><button aria-pressed={reviewTier==='promo_cuts'} onClick={()=>{setReviewTier('promo_cuts');setTierLimit(12)}}>Promo cuts<span>{cuts.length}</span></button></div><details className="s-details"><summary>Processing details and other candidates</summary><p>These lists explain what happened during processing. They are separate from the downloadable source-checked cuts above.</p><div className="review-tiers">{[['ai_suggestions','AI checks passed',suggested.length],['needs_judgment','Editor flags',judgment.length],['not_reviewed','Unfinished candidates',unreviewed.length]].map(([key,label,count])=><button key={key} aria-pressed={reviewTier===key} onClick={()=>{setReviewTier(key);setTierLimit(12)}}>{label}<span>{count}</span></button>)}</div></details></>}
    {hasRun&&!loadingRows&&!tierRows.length&&<div className="simple-panel s-empty"><Scissors size={30}/><h2>{reviewTier==='promo_cuts'?'No source-checked cuts are available yet.':'No clips in this processing list.'}</h2><p>{reviewTier==='promo_cuts'?'This run needs more completed reviews. If it paused, open Find promos to see the cause. An unfinished candidate is not a finished promo.':'Rejected and unfinished candidates remain available in the processing details and audit tools.'}</p><Button secondary onClick={()=>move(2)}>{running?'See progress':'See processing status'}<ArrowRight size={16}/></Button></div>}
    {!loadingRows&&tierRows.slice(0,tierLimit).map((r,index)=><PromoCard key={r.candidate_id} row={r} index={index} pid={r.origin_project_id||pid} onUpdated={reloadRows} onError={setError} onRevised={async()=>{await reloadRows();setReviewTier('not_reviewed');setNotice('Your new cut is saved without inherited AI scores. Reused cuts keep revisions in their originating saved run.')}}/>)}
    {!loadingRows&&tierRows.length>tierLimit&&<Button secondary onClick={()=>setTierLimit(n=>n+12)}>Show 12 more clips<ChevronDown size={16}/></Button>}
    {!loadingRows&&(suggested.length>0||kept.length>0)&&<section className="simple-panel download-box"><div><Download size={23}/><h2>{kept.length?'Your kept promos are ready.':'Download the suggestions.'}</h2><p>{kept.length?'Your kept list includes only the clips you marked Keep it, with your reasons and the cut timestamps.':'Download the AI suggestions for review, or mark the clips you want to keep first. Your editor uses the timestamps to cut the footage.'}</p></div>{kept.length>0&&<button className="s-button" onClick={()=>downloadKept(kept)}><Check size={16}/>Download my {plural(kept.length,'kept promo')}</button>}{suggested.length>0&&<a className={'s-button '+(kept.length?'secondary':'')} href={`/api/projects/${pid}/export/shortlist-csv`}><Download size={16}/>Download all AI suggestions</a>}<p className="s-fineprint">Downloads contain script content. Keep confidential show material private.</p></section>}
   </>}
   {project&&<details className="more-tools"><summary>More tools <small>Optional</small><ChevronDown size={14}/></summary><p>You don’t need these to find and download promos.</p><div><button onClick={()=>setAdvanced('scripts')}>Inspect source files</button><button onClick={()=>setAdvanced('candidates')}>Inspect other clips</button><button onClick={()=>setAdvanced('quality')}>Compare with your own picks</button><button onClick={()=>setAdvanced('exports')}>More download formats</button></div><details className="s-details"><summary>AI models and technical details</summary><p>Story analysis: {project.models.analysis}<br/>New-viewer review: {project.models.blind}<br/>Source/context check: {project.models.context}</p><p>Models judge the screenplay, not actual viewer behavior. Scores and source warnings are available inside each suggestion.</p></details></details>}
   <footer className="simple-footer"><span>20–60 second suggestions, grounded in your script.</span><button onClick={()=>setHelp(true)}>How do I use this?</button></footer>
  </main>
  {savedOpen&&<Modal title="Open saved work" onClose={()=>setSavedOpen(false)}><p>Choose a show to continue. Opening saved results makes no AI call.</p><div className="saved-list">{projects.map(p=><button key={p.id} onClick={()=>openProject(p.id)}><FolderOpen size={18}/><span>{p.name}</span><ArrowRight size={15}/></button>)}{!projects.length&&<p>No saved shows yet. Start by adding scripts.</p>}</div></Modal>}
  {help&&<Modal title="Three steps. That’s all." onClose={()=>setHelp(false)}><div className="simple-help"><p><b>1. Add scripts.</b> Upload a ZIP of your episode folder, or choose multiple episode files. They need usable timestamps.</p><p><b>2. Find promos.</b> Check the selected episodes and click Find promos. This is the step that uses AI.</p><p><b>3. Review & download.</b> Read the suggested cuts. Keep, change or reject them, then download their timestamps.</p><p>“Suggested” means the AI checks passed. Your judgment and a footage check are still needed.</p><details className="s-details"><summary>What are “your own picks”?</summary><p>These are optional clips you select yourself to compare against the AI. You can use the main app without filling them in.</p></details></div><Button onClick={()=>setHelp(false)}>Got it<Check size={16}/></Button></Modal>}
  {advanced&&project&&<div className="advanced-overlay" role="dialog" aria-modal="true" aria-label="Optional tools"><div className="advanced-bar"><button onClick={()=>setAdvanced(null)}><ArrowLeft size={17}/>Back to my promos</button><span>Optional tools</span></div><div className="advanced-view">
   {advanced==='scripts'&&<Sources project={project} pid={pid} onImport={()=>{setAdvanced(null);move(1)}}/>}
   {advanced==='candidates'&&<Shortlist initialFilter="all" rows={rows} loading={loadingRows} pid={pid} project={project} onUpdate={reloadRows} onError={setError}/>}
   {advanced==='quality'&&<Quality project={project} pid={pid} onError={setError} onNotice={setNotice}/>}
   {advanced==='exports'&&<Exports project={project} pid={pid} rows={rows}/>}
   {error&&<div className="s-alert error" role="alert">{error}</div>}{notice&&<div className="s-alert success" role="status">{notice}</div>}
  </div></div>}
 </div>;
}
function UploadStep({onImported,onError}){
 const [files,setFiles]=useState([]),[name,setName]=useState(''),[busy,setBusy]=useState(false),[drag,setDrag]=useState(false);
 async function submit(){
  setBusy(true);
  try{const data=new FormData();data.append('title',name.trim()||files[0]?.name.replace(/\.[^.]+$/,'')||'My screenplay');files.forEach(f=>data.append('files',f,f.webkitRelativePath||f.name));await onImported(await api('/projects',{method:'POST',body:data}));}
  catch(e){onError(e.message)}finally{setBusy(false)}
 }
 return <section className="simple-panel upload-step">
  <label className={`simple-drop ${drag?'drag':''}`} onDragOver={e=>{e.preventDefault();setDrag(true)}} onDragLeave={()=>setDrag(false)} onDrop={e=>{e.preventDefault();if(!busy)setFiles([...e.dataTransfer.files]);setDrag(false)}}>
   <span className="upload-symbol"><Upload size={29}/></span><strong>{files.length?`${plural(files.length,'file')} selected`:'Choose your episode files'}</strong><span>Drop a ZIP or multiple scripts here, or click to browse.</span><input type="file" aria-label="Choose episode files" multiple disabled={busy} accept=".zip,.txt,.md,.srt,.vtt,.csv,.json,.docx,.pdf" onChange={e=>setFiles([...e.target.files])}/>
  </label>
  {files.length>0&&<div className="chosen-files">{files.slice(0,3).map((f,i)=><span key={i}><FileText size={14}/>{f.name}<button disabled={busy} aria-label={`Remove ${f.name}`} onClick={()=>setFiles(values=>values.filter((_,n)=>n!==i))}><X size={13}/></button></span>)}{files.length>3&&<small>and {files.length-3} more</small>}</div>}
  <label className="folder-upload"><FolderOpen size={17}/><span>Or choose an entire episode folder</span><input type="file" aria-label="Choose episode folder" webkitdirectory="" directory="" multiple disabled={busy} onChange={e=>setFiles([...e.target.files].filter(f=>!f.webkitRelativePath.split('/').some(p=>p.startsWith('.')||p==='__MACOSX')&&/\.(txt|md|srt|vtt|csv|json|docx|pdf)$/i.test(f.name)))}/></label>
  <p className="s-note">Upload a ZIP, a folder, or selected episode files. There is no fixed episode count.</p>
  <Button onClick={submit} disabled={!files.length||busy}>{busy?<Loader2 size={17} className="spin"/>:<ArrowRight size={17}/>} {busy?'Reading your scripts…':'Check my scripts'}</Button>
  <p className="s-fineprint">This checks your files only. AI analysis starts in the next step.</p>
  <details className="s-details"><summary>File formats and optional show name</summary><p>Timed TXT/Markdown, SRT/VTT subtitles, timed CSV, canonical JSON, DOCX or text PDF with supported timing. Use one ZIP or multiple files. Limits: 4 MB per file, 32 MB per ZIP.</p><p>Untimed scripts need timestamps. Scanned PDFs need OCR before upload. We do not invent cut times.</p><label className="s-field">Show name <small>(optional)</small><input value={name} onChange={e=>setName(e.target.value)} placeholder="e.g. My show — season one"/></label></details>
 </section>;
}
function ScriptIssues({project}){
 const bad=project.ingestion.files.filter(f=>f.status==='failed'||f.status==='partial');
 const unsafe=project.episodes.filter(e=>!e.source_ready);
 const reserved=project.episodes.filter(e=>e.reference_reserved);
 if(!bad.length&&!unsafe.length&&!reserved.length)return null;
 return <details className="s-details issue-details" open={project.ingestion.source_ready_episodes===0}>
  <summary>{project.ingestion.source_ready_episodes===0?'Your files need attention before analysis':'Some episodes cannot be included. See why.'}</summary>
  {bad.map((f,i)=><p key={i}><strong>{f.source_file}</strong><br/>{f.message||'Some records could not be read. Check the source text and timestamps.'}</p>)}
  {unsafe.map(e=><p key={e.id}><strong>Episode {e.number}: timing needs attention.</strong> {e.issues.map(i=>i.message).join(' ')}</p>)}
  {!!reserved.length&&<><p>{plural(reserved.length,'episode')} are kept separate from ordinary discovery. A reservation does not establish independent evaluation; exposed episodes cannot be claimed as blind references. New shows do not inherit these episode-number restrictions.</p>{reserved.map(e=><p key={e.id}>Episode {e.number}: {e.reservation_reason||'Reference source reserved'}</p>)}</>}
 </details>;
}
function RunProgress({project}){
 const stage=project.batch?.stage||'prepare';const phase=stage.startsWith('context')?2:stage.startsWith('blind')?1:0;
 return <div className="simple-progress" role="status"><Loader2 className="spin" size={23}/><strong>{['Reading the story','Checking possible promos','Checking the source and preparing results'][phase]}</strong><p>{project.batch?.ai_analyzed_episodes||0} / {project.selected_episodes.length} selected episodes analysed · {project.batch?.blind_reviewed||0} audience reviews · {project.batch?.context_reviewed||0} source checks</p><p>{project.processing_profile==='local'?'Running on this Mac. Keep the app server running.':'Free usage limits can add a waiting period. Completed work is saved automatically.'}</p><ol>{['Read selected episodes','Review possible clips','Prepare your suggestions'].map((s,i)=><li className={i===phase?'working':i<phase?'done':''} key={s}>{i<phase?<Check size={14}/>:<span>{i+1}</span>}{s}</li>)}</ol><p>{project.job?.message}</p></div>;
}
function SourceReader({pid,episodes}){
 const [eid,setEid]=useState(episodes[0]?.id||''),[source,setSource]=useState(null),[error,setError]=useState('');
 useEffect(()=>{let alive=true;if(eid)api(`/projects/${pid}/episodes/${eid}`).then(v=>alive&&setSource(v)).catch(e=>alive&&setError(e.message));return()=>{alive=false}},[pid,eid]);
 return <div className="source-reader"><label className="s-field">Choose an episode<select value={eid} onChange={e=>setEid(e.target.value)}>{episodes.map(e=><option key={e.id} value={e.id}>Episode {e.number} — {e.filename}</option>)}</select></label>{error&&<p role="alert">{error}</p>}<div className="simple-transcript"><Transcript events={source?.events||[]}/></div></div>;
}
function Transcript({events}){return <div className="s-transcript">{events.map((e,index)=><div key={e.event_id||index}><time>{time(e.source_start_ms??e.start_ms)}</time><section><b>{e.speaker_raw||({action:'Action',camera:'Camera note',sound:'Sound'}[e.event_type])||'Scene note'}</b><p>{e.text||'This note is only partly inside the cut. Its exact content at this moment needs a footage check.'}</p></section></div>)}</div>}
function PromoCard({row:r,index,pid,onUpdated,onError,onRevised}){
 const [open,setOpen]=useState(false),[choice,setChoice]=useState(r.editor_note?.verdict||''),[name,setName]=useState(r.editor_note?.evaluator||''),[reason,setReason]=useState(r.editor_note?.reason||''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false);
 const [start,setStart]=useState(time(r.start_ms)),[end,setEnd]=useState(time(r.end_ms)),[cutReason,setCutReason]=useState('');
 async function save(e){e.preventDefault();setBusy(true);try{await api(`/projects/${pid}/candidates/${r.candidate_id}/review`,{method:'POST',body:JSON.stringify({evaluator:name,verdict:choice,reason})});setSaved(true);await onUpdated()}catch(e){onError(e.message)}finally{setBusy(false)}}
 async function revise(e){e.preventDefault();setBusy(true);try{if(!Number.isFinite(clock(start))||!Number.isFinite(clock(end)))throw new Error('Enter start and end as minutes:seconds, for example 00:12.000.');if(clock(end)-clock(start)<20000||clock(end)-clock(start)>60000)throw new Error('Choose a clip between 20 and 60 seconds.');await api(`/projects/${pid}/candidates/${r.candidate_id}/revise`,{method:'POST',body:JSON.stringify({start_ms:clock(start),end_ms:clock(end),reason:cutReason})});await onRevised()}catch(e){onError(e.message.includes('boundary')?'Use a 20–60 second cut that starts and ends between complete dialogue/action records. Check the timestamps in the script.':e.message)}finally{setBusy(false)}}
 const label=r.delivery_kind==='draft_editor_check'?'Draft promo cut':r.delivery_kind==='ai_suggestion'?'Promo cut':r.status==='ai_eligible'?'AI suggestion':r.status==='editor_review'?'Needs your judgment':'Not reviewed yet';
 const status=r.editor_note?.verdict==='accept'?'You kept this promo':r.editor_note?.verdict==='reject'?'You chose not to use this':r.editor_note?.verdict==='edit'?'You requested a change':label+' · Your review needed';
 return <article className="simple-panel promo-card">
  <div className="promo-top"><span>{label} {index+1}</span><span>Episode {Number(r.episode_id.slice(1))} <i/> {((r.end_ms-r.start_ms)/1000).toFixed(1)} seconds</span></div>
  <h2>{r.hook_quote||r.hook||r.blind?.hook||'A candidate cut from your script'}</h2>
  {r.delivery_kind&&<p className="s-note">{r.delivery_note}</p>}
  <div className="promo-range"><Scissors size={16}/><span><b>Start</b> {time(r.start_ms)}</span><ArrowRight size={14}/><span><b>End</b> {time(r.end_ms)}</span></div>
  <div className="promo-reason"><strong>{r.status==='ai_eligible'?'What keeps the viewer curious?':'Ending to inspect'}</strong><p>{r.cliffhanger_quote||r.cliffhanger||r.blind?.cliffhanger||'Read the selected scene and decide whether its ending creates a meaningful pending outcome.'}</p></div>
  {!!r.editor_review_reasons?.length&&<div className="editor-reasons"><strong>Why your judgment is needed</strong><ul>{r.editor_review_reasons.map(reason=><li key={reason}>{({partial_camera_record:'A camera note is only partly inside this cut. Check the footage.',duration_margin_under_1s:'The duration is less than one second from a limit. Check the cut timing.',question_ending_requires_editor:'The ending is a question. Check that it leaves a meaningful outcome pending.',backup_judge_requires_editor:'A backup model judged this clip after the primary model failed.'})[reason]||reason.replaceAll('_',' ')}</li>)}</ul>{!r.context_review_status||r.context_review_status==='pending'?<p>The source/context review is not complete.</p>:null}</div>}
  <p className="decision-state"><span className={r.editor_note?'marked':''}/>{status}</p>
  <button className="read-clip" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?'Close clip details':'Read this clip & decide'}<ChevronDown size={17}/></button>
  {open&&<div className="open-clip">
   <h3>Read the selected scene</h3><p className="s-note">These are script records inside this cut. Your editor should also check the actual footage.</p><div className="simple-transcript"><Transcript events={r.selected_content||[]}/></div>
   <div className="your-decision"><h3>Would you use this promo?</h3><div className="simple-decisions">{[['accept','Keep it'],['edit','Needs changes'],['reject','Don’t use it']].map(([key,label])=><button aria-pressed={choice===key} key={key} className={choice===key?'selected':''} onClick={()=>{setChoice(key);setSaved(false)}}>{choice===key?<Check size={15}/>:null}{label}</button>)}</div>
    {choice&&<form onSubmit={save}><label className="s-field">Your name<input required value={name} onChange={e=>setName(e.target.value)} placeholder="Who reviewed this clip?"/></label><label className="s-field">What made you choose this?<textarea required value={reason} onChange={e=>setReason(e.target.value)} placeholder="Your reason, or the change you want."/></label><Button disabled={busy}>{busy?<Loader2 className="spin" size={16}/>:<Check size={16}/>}Save my decision</Button>{saved&&<p className="s-saved" role="status">Your decision is saved. The AI’s original result is unchanged.</p>}</form>}
   </div>
   <details className="s-details"><summary>AI review notes and scores</summary><p>{r.why_it_works||r.blind?.why_it_works||'No completed audience judgment yet.'}</p><p>{r.post_first_reason}</p><p>These are screenplay judgments, not predictions of ad performance. Incomplete or uncertain reviews do not establish eligibility.</p><details className="s-details"><summary>Show scores and review notes</summary><dl className="s-scores">{[['Opening hook','hook_score'],['Unresolved ending','cliffhanger_score'],['Easy to understand','standalone_clarity_score'],['Emotional stakes','emotional_stakes_score']].map(([label,key])=><div key={key}><dt>{label}</dt><dd>{r.blind?.[key]??'Not assessed'} / 10</dd></div>)}</dl>{r.context?.reasons?.map((text,i)=><p key={i}>{text}</p>)}<p>Warnings: {r.warnings?.length?r.warnings.map(v=>v.replaceAll('_',' ')).join('; '):'None recorded'}</p><p>Audience judge: {r.judged_by?.blind?.model||r.model_blind||'Not recorded'}{r.judged_by?.blind?.fallback_used?' (backup)':''}</p><p className="s-fineprint">{r.candidate_id} · Review configuration {r.config_hash}</p></details></details>
   <details className="s-details"><summary>Change the start or end time</summary><p>A different cut needs fresh review. It will be saved separately without old AI scores or approval.</p><form onSubmit={revise}><div className="simple-time-fields"><label className="s-field">Start<input required value={start} onChange={e=>setStart(e.target.value)}/></label><label className="s-field">End<input required value={end} onChange={e=>setEnd(e.target.value)}/></label></div><label className="s-field">Why are you changing the cut?<textarea required value={cutReason} onChange={e=>setCutReason(e.target.value)}/></label><Button secondary disabled={busy}>Save as a new cut<Scissors size={15}/></Button></form></details>
  </div>}
 </article>;
}
function Modal({title,onClose,children}){return <div className="simple-modal-background" onClick={onClose}><section className="simple-modal" role="dialog" aria-modal="true" aria-label={title} onClick={e=>e.stopPropagation()}><button className="s-close" aria-label="Close dialog" onClick={onClose}><X size={19}/></button><h2>{title}</h2>{children}</section></div>}
function downloadKept(rows){
 const fields=['episode','start','end','seconds','opening_line','cliffhanger','reviewed_by','your_reason','candidate_id','ai_review_status','config_hash'];
 const quote=value=>'"'+String(value??'').replaceAll('"','""')+'"';
 const lines=[fields.join(','),...rows.map(r=>[r.episode_id,time(r.start_ms),time(r.end_ms),(r.end_ms-r.start_ms)/1000,r.hook_quote||r.hook,r.cliffhanger,r.editor_note.evaluator,r.editor_note.reason,r.candidate_id,r.status,r.config_hash||''].map(quote).join(','))];
 const url=URL.createObjectURL(new Blob([lines.join('\n')],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='my-kept-promos.csv';a.click();URL.revokeObjectURL(url);
}
createRoot(document.getElementById('root')).render(<App/>);
