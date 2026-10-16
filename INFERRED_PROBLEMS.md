# Signal — inferred problems (addendum to FEED_ALGORITHM.md)

Add this to `FEED_ALGORITHM.md` as **section 17**, or keep it as a separate file next to it. Build it after the phase currently in progress. Same conventions apply: every threshold marked *(tune)* lives in `config/feed.yaml`.

---

## 17. Inferred problems

### 17.1 Why

Most posts in the feed are **reported problems**: someone states a pain directly. The best startup problems are often never stated publicly, because insiders don't post about them. Inferred problems close that gap by combining several public signals that are each explicit about something else, so the problem appears where they intersect.

Rule: **the AI connects evidence, it never invents it.** Every inferred problem must carry a chain of real, linked evidence. No evidence chain, no inferred problem.

### 17.2 Output: InferredProblem

| Field | Notes |
|---|---|
| `id` | |
| `kind` | `inferred` (reported problems keep `kind = reported`) |
| `detector` | which detector produced it (17.3) |
| `hypothesis` | one plain sentence: "Small trucking fleets re-enter driver logs by hand to meet the new FMCSA rule" |
| `who` | role and business type |
| `niche_id` | leaf niche in the niche tree |
| `evidence` | list of `{post_id or record_id, source, url, what_it_shows}`; at least 3 items from at least 2 source types *(tune)* |
| `counter_evidence` | anything found that weakens it (existing tools, contradicting posts) |
| `confidence` | `high` / `medium` / `low` (17.6) |
| `critic_notes` | the critic pass's verdict and reasons |
| `validation` | insider votes and builder field notes (17.7) |
| `status` | `candidate`, `published`, `rejected`, `expired` |
| `created_at`, `refreshed_at` | |

Inferred problems are scored with the same problem score as reported ones (section 5.2), then shown with a distinct label.

### 17.3 Detectors

Build in this order. Detectors 1 and 2 are the MVP; 3–6 come after they prove useful.

**Detector 1 — Paid workarounds** (MVP)
- Inputs: job posts, freelance gigs, template marketplaces and automation template listings already collected by the connectors.
- Signal: people paying a human, or buying a spreadsheet/template, to do a repetitive task.
- Logic:
  1. Enrichment tags each item with `task` (normalized verb phrase, e.g. "reconcile vendor invoices against POs"), `role`, `industry_path`, `money_signal`.
  2. Cluster items by task embedding within a niche.
  3. A cluster becomes a candidate when it has ≥ 5 paid items from ≥ 3 different buyers in 60 days *(tune)*.
  4. Search the existing problem catalog and software-review data for tools that already do this task; record them as counter-evidence.
- Hypothesis template: "[Who] pay people to [task] by hand because no tool does it well."
- **As built (2026-10-15):** `signal_feed/inferred/paid_workarounds.py`, `scripts/inferred.py run`. Inputs: `paid_demand` posts in the last 60 days (tenders from SAM.gov, TED and UK; job posts from `job_boards`; GitHub bounties). Template marketplaces and automation-template listings aren't collected by any connector yet. Tasks come from the v9 `task` field, or, until posts are re-tagged, from the pain statement with the buyer and lead-in stripped ("The council needs a contractor to service its fleet" → "service its fleet"; such candidates are marked `derived`). Tasks rated `task_automatable` < 0.5 are skipped (repairs, construction). Clusters: average linkage at cosine ≥ 0.80 within each leaf niche, stored in `task_clusters`. Buyers: the author hash, or for tenders and job posts the buying organization (SAM.gov: its agency path, since the department is too coarse). Every evidence line is built from the post itself ("Council 2 pays for: redline vendor contracts (£40k)"). Counter-evidence: catalog posts within cosine 0.6 that are positive reviews, tool listings, launches or complaints about a named tool (reviews and listings are now embedded for this), and the tools the buyers name themselves. First run on the real corpus (1,823 paid posts, derived tasks): 1,248 task clusters, 0 candidates. The clusters big enough came from one portal only (Defense Logistics Agency parts on SAM.gov), which fails the 2-source-type minimum, and buying parts isn't a software workaround anyway. Job posts and gigs, the detector's main inputs, are not tagged or collected yet.

**Detector 2 — Rule change × affected role** (MVP)
- Inputs: Federal Register, Regulations.gov, state legislature trackers, bar/medical/licensing board notices; job posts.
- Signal: a new or proposed rule that creates work for a specific role.
- Logic:
  1. For each new rule document, extract with the LLM: who must comply, what they must now do, the deadline, and the industries affected. Store each as a structured `obligation`.
  2. Match obligations to roles via job-post duties in the same industry (embedding match on duties vs. obligation).
  3. Candidate when an obligation maps to ≥ 1 role with ≥ 10 recent job posts *(tune)* and the deadline is in the future.
  4. Look for public comments on the rule (Regulations.gov) from affected businesses as supporting evidence.
- Hypothesis template: "[Rule] requires [who] to [obligation] by [date]; today this is done by [role] manually."
- **As built (2026-10-15):** `signal_feed/inferred/rule_change.py`. Obligations come from v9 tagging of rule documents (`obligations` table: who, what, deadline, industries), embedded as "who: what". Job posts are posts from `job_boards` or HN's job community in the last 90 days, grouped by role: the title with seniority and location removed ("Senior Payroll Specialist - Remote" → "payroll specialist"). A role matches an obligation when ≥ 10 of its posts in the obligation's industries are within cosine 0.45 of it (a starting guess, in config). Only obligations with a stated, future deadline count; a deadline is never inferred. Evidence: the rule ("<title>: requires <who> to <what> by <date>"), up to 5 matching job posts ("<employer> is hiring: <title>") and up to 5 Regulations.gov comments on the rule's docket (rules list `docket_ids`, comments carry `docket`). It goes through the same critic, confidence and publishing as Detector 1. On the real corpus it finds nothing yet: obligations need the v9 re-tag (Anthropic credit), and only 73 job posts exist so far. **Update (2026-10-15, after the v9 re-tag):** 348 obligations extracted, but 326 state no deadline and only 65 job posts exist, so the job route found nothing. Changes: (1) an obligation with no stated deadline counts while its rule is ≤ 180 days old (a past deadline still excludes it); (2) when no role has enough job posts, the affected people's own posts stand in as the role evidence: pain and tool-gap posts whose *pain statement* is within cosine 0.60 of the obligation, at least 3 from different authors, shown as "<role> describes a related problem: …" (0.55 on whole-post embeddings let in topical neighbours); the template then says "the people affected already struggle with related work"; (3) one candidate per rule: obligations within cosine 0.88, or on the same docket, are merged, preferring the rule's own document over a public comment, and a comment-derived rule is named "<agency>'s rule on docket <id>". Result: 5 candidates (Let's Encrypt 64-day certificates, FTC impersonation rule, FMCSA English proficiency, FMCSA sleeper-berth pilot, an SEJ article on Claude's policy that the critic should reject), held for the critic.

**Detector 3 — Gaps between tools**
- Inputs: reviews, feature boards, community posts.
- Signal: "export to CSV", "integration with X", "copy from A into B" complaints.
- Logic: extract `(tool_a, tool_b, data_moved)` triples; cluster; candidate when ≥ 8 mentions from ≥ 2 source types *(tune)*.
- Hypothesis template: "Users of [A] and [B] move [data] between them by hand."

**Detector 4 — Labor pressure**
- Inputs: job posts over time (volume and stated pay).
- Signal: rising volume or pay for a role whose duties are mostly routine.
- Logic: per role × industry, compare last 90 days to the prior 90; LLM rates the routine share of listed duties. Candidate when volume or pay rises ≥ 20% *(tune)* and routine share is high.
- Hypothesis template: "Demand for [role] is rising; [routine duties] are candidates for automation."

**Detector 5 — Ignored requests**
- Inputs: public feature boards, GitHub issues.
- Signal: requests with many votes, open for a long time, no vendor response.
- Logic: candidate when votes ≥ 50 *(tune)* and age ≥ 12 months with no status change.
- Hypothesis template: "[Tool] users have asked for [feature] for [time]; nobody serves them."

**Detector 6 — Cross-industry transfer**
- Inputs: the problem catalog and software review categories.
- Signal: a workflow well served by software in one industry, unserved in a similar one.
- Logic: find task clusters present in two industries; compare tool coverage (review counts, vendor count). Candidate when one side has many tools and the other has workaround evidence (Detector 1).
- Hypothesis template: "[Task] has good tools in [industry A] but [industry B] still does it by hand."

### 17.4 Pipeline

```
connectors → enrichment (adds task, obligation, tool-gap fields)
          → detectors (weekly batch; Detector 1 also daily)
          → candidate InferredProblem with evidence
          → critic pass
          → confidence scoring
          → publish to feed (or reject)
          → insider and builder validation
          → detector weights updated
```

**Rebuilt (2026-10-15): anchors and corroboration.** After the v9 re-tag, the counting detectors found almost nothing: the corpus has many authoritative records (348 rule obligations, ~1,100 change posts, ~3,200 tenders) and few posts from people describing their own work (Reddit, LinkedIn and most forums are closed to us), spread over 27 industries. Clusters big enough came from one source type (tenders), which the evidence minimum correctly stops. Two additions, with the minimum, the critic and confidence unchanged:

- **Near misses get corroborated.** A detector now returns candidates that miss the minimum as `near_miss` instead of dropping them. Each gets one planning call (`inferred/corroborate.py`, Haiku, cached per candidate): is this people doing or paying for routine work software could take on (`labor`), or buying software or goods? Only `labor` goes on, to one targeted X search for the affected people's own posts (`signal_connectors.ondemand.search_x`, built by the connectors session). Found posts are stored in signal.db by the connector and quoted as evidence lines ("A post on X: …"). Only their ids are cached here (`inferred_corroboration`, refreshed after 14 days), so deletions at X drop them from evidence. X posts are a second source type; the critic checks each one. Caps per detector run: 30 plans, 15 searches (~$0.05 each). Without an LLM or without the connector, near misses are counted and dropped as before.
- **Anchor-first detector** (`inferred/anchored.py`, trial). It starts from one fact: a `change` post outside the rule sources (platform and vendor changes, recalls, launches; rules go through Detector 2's obligations) or a tender whose main classification code is human labor (CPV data entry/processing, business and admin services; NAICS legal, accounting, consulting, office admin, business support). Tender codes come from the notices themselves (`common.tender_kind`); 1,235 of 1,456 TED notices are software purchases. The planning call writes the claim ("<who> now have to <task> because <fact>; today they do it by hand") or declines; corroboration is the corpus's pain statements close to the anchor (cosine 0.60, one per author) plus the X search. Anchors already planned come back every run at no cost; new ones are offered 40 per run, those with corpus voices first and a quarter of the slots for labor tenders.
- **Detector 1** skips near misses whose tenders are mostly software or goods purchases (no planning call wasted on them).

### 17.5 Critic pass

A second, separate LLM call receives only the hypothesis and the linked evidence (not the detector's reasoning) and must:
1. Check that each evidence item actually supports the claim it is attached to; drop items that don't.
2. Propose the strongest alternative explanation.
3. Search the catalog and reviews for an existing solution (counter-evidence).
4. Return `keep` / `revise` / `reject` with reasons.

Reject automatically if, after dropping unsupported items, fewer than 3 evidence items from 2 source types remain *(tune)*.

**As built (2026-10-15):** `signal_feed/inferred/critic.py`. One Claude call (the tagger model) gets the claim, the numbered evidence lines with a short excerpt of each post, and the possible existing tools found in the catalog. It never sees the detector's cluster, buyer counts or reasoning. Its answer: per-item supports / doesn't, an alternative explanation, an existing solution or null, and keep / revise / reject. An item the critic didn't vouch for counts as unsupported. Unsupported items stay stored (`supported = false`) but are never shown. **Nothing is published without a critic run** (`inferred.publish_without_critic: false`): without Anthropic credit, candidates are found and stored as `candidate`. A published problem keeps its status when a later run can't reach the critic.

### 17.6 Confidence

| Level | Rule *(tune)* |
|---|---|
| High | ≥ 8 evidence items, ≥ 3 source types, critic `keep`, no strong existing solution, or confirmed by ≥ 2 insiders |
| Medium | meets minimum evidence, critic `keep` or `revise` |
| Low | minimum evidence only; shown only in exploration slots and labeled "early signal" |

**As built:** `signal_feed/inferred/common.confidence`. "No strong existing solution" means no catalog post was found as counter-evidence and the critic named none.

### 17.7 Validation loop

- **Insiders:** users who say they work in an industry get one short prompt per week about inferred problems in their niche: "Is this real where you work? Yes / No / Partly + one line." Confirmations raise confidence; rejections lower it and are fed back to the detector.
- **Builder field notes:** when builders log customer conversations in the outreach flow, notes that mention an inferred problem attach to it as evidence (with the builder's permission).
- **Learning:** track, per detector and per signal combination, the share of published inferred problems that insiders confirm and that lead to saves, ideas and outreach. Lower the weight of combinations that keep failing; raise those that hold up.

**As built (2026-10-15):** `signal_feed/inferred/validation.py`.
- *Insiders* are users who name the industries they work in at onboarding (`works_in`, separate from their interests). Someone interested in Legal but not working in it is not an insider.
- *The prompt:* `GET /inferred/prompt` returns at most one question per 7 days: the best published inferred problem in their industry they haven't been asked about or voted on, with its first 3 evidence items. Answers go to `POST /inferred/{id}/vote` (yes / no / partly + a note of up to 300 characters, personal data scrubbed; a later vote replaces the earlier one).
- *Effect:* only insider votes count. 2 insider "yes" make it **high** (17.6). Net insider "no" (no − yes − ½ partly) ≥ 2 lowers it one level, and ≥ 3 rejects it once it is low (recorded as `rejected_by_insiders`). "Not for me" with reason `not_true` ("Not true in my experience", 17.8) on an inferred item records a "no" vote and leaves the user's niche affinity alone.
- *Field notes:* `POST /inferred/{id}/field-notes` attaches a builder's customer-conversation note as evidence (source type `field_note`) only when `share` is true. Personal data is scrubbed, and the note can raise the evidence count and confidence.
- *Full chain:* `GET /inferred/{id}` returns the evidence and counter-evidence ("+N more").
- *Learning:* `scripts/inferred.py weights` (weekly). Per detector × evidence source-type mix, `weight = 2 × (held up + 1) / (judged + 2)`, clamped to 0.3–1.5. A problem holds up when insiders confirmed it or someone saved it, turned it into an idea or reached out; it fails when insiders rejected it; critic rejections don't count. No data gives 1. Inferred items' ranking scores are multiplied by their weight, which is logged in `score_breakdown.detector_weight`.
- *17.10 metrics* are in `scripts/feed_metrics.py` under `inferred_problems`: insider confirmation rate per detector, critic rejection rate, share with no existing solution found, and save and idea rates of inferred vs reported items.

### 17.8 How it appears in the app

- Label: **Inferred** (vs. Reported), with the confidence level.
- The card shows the hypothesis and a short "Why we think this" list of 3 evidence items with links; "+N more" opens the full chain.
- Ranking uses the same model as other items, with a feature for `kind` and `confidence`. Low-confidence items only appear in exploration slots.
- "Not for me" reasons gain one option for inferred items: "Not true in my experience" (routes to validation, not just ranking).

**As built (2026-10-15):** published inferred problems are feed items (`item_type: inferred`). Their vector is the mean of the evidence posts' embeddings, their features are the evidence posts' averages, and their problem rank is computed among inferred problems. Feed items carry `kind` (reported | inferred), `confidence`, `evidence` (the first 3 supported items with links) and `evidence_more`. The summary is the hypothesis, and `post_id` is null (use `item_type` and `item_id` for events). Ranker features `inferred` and `confidence_level` carry small coefficients. Low-confidence items enter only as exploration candidates, including in the user's own niches, which is their only way in. Events with `item_type: inferred` update the problem's niche. The "Not true in my experience" option comes with insider validation (build step 4). The evidence list is content ("why we think this problem exists"), not an explanation of personalization, so it doesn't conflict with the feed's "no visible reasons" rule.

### 17.9 Data model additions

- `inferred_problems`, `inferred_evidence`, `obligations` (from rule documents), `task_clusters`, `insider_votes`, `detector_stats`
- Enrichment fields added to `enriched_posts`: `task`, `tool_mentions`, `data_moved`, `obligation_ids`

### 17.10 Measuring it

- Insider confirmation rate per detector (target ≥ 50% for published items *(tune)*)
- Save and idea-conversion rate of inferred vs. reported items
- Share of inferred problems with no existing solution found
- Critic rejection rate per detector (too high means the detector is noisy)

### 17.11 Build order

1. Enrichment fields (`task`, `obligation`, `tool_mentions`).
2. Detector 1 + critic pass + confidence + "Inferred" label in the feed API.
3. Detector 2 (rule obligations extraction and role matching).
4. Insider prompts and validation storage.
5. Detectors 3–6, one at a time, each shipped only if its insider confirmation rate holds up.

**Status (2026-10-15):** steps 1–5 are built, tests pass, everything is committed.
- **Stages:** detectors 1–2 are `live`. Detectors 3–6 are `trial` (`inferred.stages`): their published problems go only to insider prompts, never the feed, and a trial detector goes live by itself once ≥ 10 of its problems are insider-rated with ≥ 50% confirmed (17.10's target).
- **Detector 3** (`tool_gaps.py`): v9 `data_moved` triples grouped by tool pair, ≥ 8 posts from ≥ 2 source types. A catalog listing or review naming both tools is the first counter-evidence (a possible integration).
- **Detector 4** (`labor_pressure.py`): job posts per role × industry, last 90 days vs the 90 before. Volume or median stated pay must be up ≥ 20% with ≥ 10 current posts, and the routine share (mean v9 `task_automatable`) must be ≥ 0.6. People in that role describing the work add a second source type. The counted change ("12 job posts vs 6 before") is shown as a summary line that never counts as evidence.
- **Detector 5** (`ignored_requests.py`): GitHub issues and discussions with ≥ 50 reactions or upvotes, ≥ 12 months old, still open (discussions: no accepted answer), plus complaints naming the same tool. Whether the vendor replied isn't in the collected data, so it is left to the critic. Public feature boards aren't collected.
- **Detector 6** (`cross_industry.py`): Detector 1's qualifying task clusters, with ≥ 3 tool posts within cosine 0.75 of the task in another industry and none in the cluster's own.
- `scripts/inferred.py run` runs all six in order (Detector 6 reads Detector 1's clusters).

**What is blocking real output:**
1. Anthropic credit, for the v9 re-tag (task, task_automatable, data_moved, obligations) and for the critic. Without the critic nothing is published.
2. Inputs: job posts (73 so far, untagged), freelance gigs and template marketplaces (not collected), year-old popular GitHub issues (the connector collects new ones), and public feature boards (deferred). First run on the real corpus: 0 candidates from every detector.
