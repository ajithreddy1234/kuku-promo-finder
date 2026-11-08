import React from 'react';

export default function ProcessingSummary({project,cuts}){
 const batch=project.batch,coverage=batch?.coverage_pass;
 const reused=cuts.filter(c=>c.reused_saved_cut);
 const origins=[...new Map(reused.map(c=>[c.origin_project_id,c.origin_run_summary||{id:c.origin_project_id}])).values()];
 const known=n=>n==null?'Not recorded':n;
 return <section className="coverage-note" aria-label="Where these results came from">
  <strong>Processing in this run</strong>
  <p>{batch?.ai_analyzed_episodes||0} of {project.selected_episodes.length} selected episodes analyzed · {batch?.blind_reviewed||0} audience reviews · {batch?.context_reviewed||0} source checks.</p>
  {coverage&&<p>{coverage.reviewed_episodes} of {coverage.episodes_with_candidates} first-choice clips have an audience review in this run. This counts clip reviews, not scripts read.</p>}
  {batch&&(batch.blind_reviewed||0)===0&&<p><b>No promo candidates have been reviewed in this run.</b> A completed story scan does not mean the promo checks are complete.{batch.review_budget_allocation?.allocated_pool===0?' No new reviews fit the estimated remaining model quota.':''}</p>}
  {coverage&&<details className="s-details"><summary>Why this run includes {coverage.eligible_episodes} episodes</summary><p>{project.episodes.length} uploaded; {coverage.excluded_episode_numbers.length} excluded by this run’s evaluation settings; {coverage.source_exclusions.length} excluded for timing. These are saved settings for this run, not a required episode limit for new uploads.</p><p>Analysis coverage and review coverage are separate. {batch.whole_show_results?'Every first-choice candidate in the eligible pass has an audience review.':'This run has not completed a whole-show promo review.'}</p></details>}
  {reused.length>0&&<div className="saved-cut-origin"><strong>{reused.length} displayed cuts come from an earlier run</strong><p>They are saved results for byte-for-byte identical script files. They are not new results from the review counter above. Reusing them makes no new AI call and does not increase this run’s progress.</p>{origins.map(o=><details className="s-details" key={o.id}><summary>Earlier run: {o.name||o.id}</summary><p>{known(o.analyzed_episodes)} episodes analyzed · {known(o.audience_reviews)} audience reviews · {known(o.source_checks)} source checks across that earlier run. These totals do not mean every episode completed both reviews.</p><p>Source run ID: <code>{o.id}</code></p></details>)}</div>}
 </section>;
}
