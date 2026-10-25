# FilmGraph product, revenue and operating plan

Prepared 22 October 2026. Prices below are provider list prices or explicitly labelled planning estimates, not a service-capacity guarantee or a quotation for data licences.

## The product to build

Start with Malayalam cinema: a conversation next to a bounded, interactive graph. A question such as “How many films did Mohanlal act in that Sreenivasan wrote?” becomes a count intent plus two people and two roles. Resolve their identifiers, fill a permitted query template, count distinct films, and return the same result set as chat cards and graph nodes. Display the interpretation so a user can correct a role or name.

The current implementation has a browser dictionary/intent parser, a server fallback, independent server plan validation, source-linked film details, song credits, location/technical evidence, graph filters/clusters, private-list integration, and a graph-overlap recommendation baseline. It is not a general conversational LLM or an exhaustive, verified filmography. IMDb principal credits do not constitute all credits. An import failure or an unresolved identity can change a collaboration count.

Do not download the full corpus, plots or graph onto every phone. Ship the UI and a compact name dictionary. Return 20 cards per page and a graph of at most 80 films with selected credits; fetch full film details on demand. The graph in the browser is a view over the results, not the database.

## Query execution and the optional browser model

```text
Question
  -> lightweight intent classification + dictionary NER
  -> resolve aliases / ask about genuinely ambiguous names
  -> JSON plan with permitted intent, people, roles, dates and clues
  -> server validates the plan and checks quota
  -> parameterized SQL graph joins + optional story retrieval
  -> distinct count + cited film cards + bounded graph
```

Use templates for collaborations, per-person filmographies, role combinations, year ranges, song searches, locations, technical mentions and film lookup. Add shortest paths or multi-hop queries only when the need is demonstrated. Never execute model-generated SQL/Cypher or accept a client-supplied user ID as authentication.

The normal path has no per-query LLM fee. The optional `/model-lab.html` experiment uses pinned WebLLM and a quantized SmolLM2 360M model after an explicit download. The upstream configuration estimates about 376 MB of VRAM for its f16 variant, which is not the download size or total application memory. Browser support, GPU capabilities, heat/battery, initial download and Malayalam accuracy vary. A small model should produce a constrained plan, not assert film facts. Benchmark it on Malayalam and English names, spelling mistakes, code-mixed questions and unknown entities before enabling it as a fallback. The lab has not been performance-validated on a physical user device in this session. [WebLLM documentation](https://webllm.mlc.ai/docs/user/get_started.html), [upstream model configuration](https://github.com/mlc-ai/web-llm/blob/main/src/config.ts).

## Accounts, recommendations and alerts

Initial policy: guests get 10 distinct searches per UTC day; confirmed signed-in accounts get 100. Repeats and pagination reuse the same query fingerprint. Store counters centrally for serverless deployments; a browser counter or per-instance SQLite file is insufficient. Anonymous IP-based limits are best effort: shared networks share a limit, and VPNs can evade it. Add WAF rate controls and verified-account abuse protection before a broad launch.

Keep favourites, watchlist, watched films, ratings, followed people/films/companies and consent preferences private through ownership-based RLS. Separate these records from public catalogue data. Account entitlements and future paid-plan limits must come from trusted server data, never editable user metadata.

Start recommendations with shared directors, writers, actors, genres and music credits, plus diversity and already-watched exclusion. Explain “because you liked X” and keep sponsored results separate. Add collaborative filtering only after sufficient voluntary ratings exist; measure Precision@K/NDCG, catalogue coverage and diversity using held-out interactions. An empty or opted-out profile gets nonpersonalized suggestions.

Following and a notification schema are prepared. **News ingestion and email/push delivery are not connected.** Production needs permitted RSS/licensed feeds, exact entity matching, deduplication and a scheduled event matcher. Store headlines, dates and source links rather than copying articles. Separate release announcements from changed catalogue metadata. A daily digest is cheaper and less intrusive than an email per event. Provide per-follow and global unsubscribe controls. Do not send marketing to users merely because they created an account.

“Recent catalogue title” is not “currently running in cinemas.” OTT availability requires a territory, provider offer, offer type and last-checked timestamp. Without a current permitted feed, show a search link instead of an availability claim.

## OTT links and revenue

An ordinary Netflix, Prime Video, YouTube or JustWatch link does **not** automatically pay us. Commission requires an approved agreement, eligible market/campaign, attribution links and a qualifying event. A recommendation is not itself a billable conversion.

JustWatch advertises partner streaming data, widgets and commission opportunities; Indian availability, eligibility, pricing and commissions need a written partner offer. No rate or contract has been established for this app. [JustWatch partner offering](https://www.justwatch.com/us/JustWatch-Streaming-API).

The cleanest initial business models are:

1. A paid enthusiast tier for larger limits, saved investigations, export, advanced graph queries and better alerts.
2. Clearly disclosed sponsorships or contextual ads, separated from organic rankings.
3. Approved affiliate/referral links where a provider actually supports this market.
4. Aggregated audience or catalogue insights for businesses, only after a lawful purpose, data rights, real aggregation protections and explicit privacy design are established.

Do not base the initial business on selling identifiable viewing histories, watchlists or search logs. A terms checkbox is not blanket permission for every future purpose. If an optional new purpose is introduced, obtain a separate, informed choice and preserve a useful nonpersonalized service.

Illustrative revenue calculation, not a forecast: 10,000 monthly users × 5 outbound clicks × 2% eligible conversion × ₹50 hypothetical commission = ₹50,000 gross. Change any assumption and the result changes; an unapproved programme pays ₹0. Likewise 300 subscribers at ₹99/month would be ₹29,700 gross before tax, payment fees, refunds, service and data costs. No subscription or affiliate collection is enabled in the pilot.

## Data rights are a separate launch dependency

- Wikidata structured data is CC0. Preserve identifiers and claim provenance.
- Wikipedia text needs its attribution/share-alike obligations observed. Keep revision URLs; isolate reusable text from code, proprietary user data and commercial feeds.
- The IMDb files used locally are for personal/noncommercial use. They are not a free commercial feed. The default deployment snapshot removes imported IMDb dataset claims while retaining externally sourced identifiers/links. Commercial IMDb use needs appropriate rights. [IMDb datasets](https://data.imdb.com/non-commercial-datasets/).
- Letterboxd's requested API access currently excludes this kind of personal/LLM/analysis project. No Letterboxd catalogue was scraped. [Letterboxd API access](https://letterboxd.com/api-beta/access/).
- Do not assume that TMDB is a free data source for a commercial or AI recommendation product. Its current terms distinguish developer and commercial use and contain AI/ML restrictions; obtain a written agreement covering the intended use. [TMDB terms](https://www.themoviedb.org/api-terms-of-use), [TMDB FAQ](https://developer.themoviedb.org/docs/faq).

The largest unknown recurring expense may be commercial metadata/OTT/news rights, not database compute.

## Monthly costs and capacity

Vercel currently lists Pro at $20/month with usage credit, and Hobby is restricted to personal/noncommercial use. Supabase lists Pro from $25/month, with one Micro covered by its compute credit, 100,000 included MAU and specified storage/egress allowances. These are billing allowances, not promises that one Micro instance supports 100,000 simultaneously active users. Supabase Free lists 50,000 MAU but only 500 MB database space and 5 GB egress. [Vercel pricing](https://vercel.com/pricing), [Vercel Hobby terms](https://vercel.com/docs/plans/hobby), [Supabase pricing](https://supabase.com/pricing).

An alternative is Workers + D1 for template/SQL serving; Cloudflare documents usage-based serverless pricing. Migrating the present Python application is extra engineering and should not delay validation of the product. [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).

The following are **planning envelopes**, excluding GST/taxes, paid data licences, labour, model API calls, large email/SMS campaigns and third-party search charges. INR figures use a deliberately conservative budgeting factor of ₹100 per USD, not a live exchange rate.

| Stage | Workload assumption | Planning budget / month | What must be verified |
|---|---|---|---|
| Private prototype | Hundreds of testers, light traffic | $0–25 / ₹0–2,500 | Free-tier limits, account email delivery, storage |
| Small commercial launch | 1,000–10,000 MAU, 50 searches/user/month | $50–150 / ₹5,000–15,000 | Query CPU, response bytes, auth/edge invocations, peak traffic |
| Growing product | 100,000 MAU, 50 searches/user/month | $200–1,000+ / ₹20,000–100,000+ | Peak load, cache hit rate, DB size/compute, alert volume, provider contracts |

These ranges are engineering estimates, not measured production capacity. Do not tell users the current prototype already supports those stages.

At 10,000 MAU × 50 searches, there are 500,000 monthly searches: about 0.19/sec averaged over 30 days, but a 100× burst is about 19/sec. At 100,000 MAU the same assumptions produce 5 million searches, about 1.93/sec average and 193/sec at 100×. Each search can make multiple API/DB operations; MAU is not request count. Load-test the **peak** with real corpus queries, cold starts, auth/quota calls, payload sizes and concurrent reads.

Using your 30-million-person Kerala planning assumption, 0.1% would be 30,000 people, not automatically 30,000 monthly active users. Validate retention and sessions before sizing for the entire population.

In Mumbai, Vercel's published fluid-compute rates currently show $0.140/CPU-hour and $0.0116/GB-hour. As an illustration, 500,000 searches at 50 ms active CPU each would consume about 6.94 CPU-hours before additional work; network waits, memory duration, traffic and included credits must also be accounted for. Measure the 50 ms assumption—it is not the observed production runtime. [Function resource pricing](https://vercel.com/docs/functions/usage-and-pricing).

An indexed relational representation of this modest public graph can answer shared-credit queries without paying for a dedicated graph cluster. Add vectors only for plot/scene clues, generate embeddings offline, and invoke a cloud LLM only for opt-in unresolved questions if it proves useful. Cache common public results; never put private list responses into a shared public cache.

## Privacy and terms

The pilot has separate controls for personalisation and saved lists, data export, local clearing, and prepared account deletion. Draft terms and privacy pages are in the app. **They are not a completed compliance assessment.** Before public launch, fill in the operator and grievance/privacy contact, review retention and processor arrangements, implement verified deletion/backup retention and alert opt-outs, and review the applicable treatment of minors.

India's DPDP Rules have phased commencement; do not treat every substantive rule as already in force on 22 October 2026. The official notification schedules different provisions immediately, after one year and after eighteen months. Review the exact applicable commencement and legal obligations before launch. An adults-only pilot reduces product scope but a checkbox is not a complete age-assurance or child-data compliance system. [Official DPDP Rules notification](https://www.meity.gov.in/static/uploads/2025/11/53450e6e5dc0bfa85ebd78686cadad39.pdf).

## Comparable projects and public services

| Example | Type / coverage | What to study |
|---|---|---|
| [Oracle of Bacon](https://www.oracleofbacon.org/) | Public service, Hollywood-oriented people/film connections | Actor/director/producer/writer paths, role and company filters, network views |
| [Neo4j Movies](https://github.com/neo4j-graph-examples/movies) and [public read-only demo guidance](https://neo4j.com/docs/getting-started/appendix/example-data/) | Open-source sample graph and a public demo; a small example dataset | Clear Person → credit → Movie relationships and source code |
| [Neo4j Recommendations](https://github.com/neo4j-graph-examples/recommendations) | Open-source movie recommendation example | Rating/user graphs and recommendation queries |
| [MovieLens](https://movielens.org/) | Public, noncommercial recommendation research service | Explicit ratings, taste profiles, explainable discovery; its research data is not automatically commercial training data |
| [Movary](https://github.com/leepeuker/movary) | Open-source, self-hosted tracking | Watched-film history, ratings and portability; not a public collaboration graph |
| [Seerr](https://github.com/seerr-team/seerr) | Open-source discovery/request manager for personal media servers | Accounts, discovery and notification UX; not an OTT licensing solution |
| [JustWatch India](https://www.justwatch.com/in) | Public service covering Indian and international titles | Territory/provider filters, new/upcoming availability, where-to-watch UX |
| [OTTplay](https://www.ottplay.com/) | Public Indian/regional OTT aggregator and editorial service | Regional discovery, cinema news and OTT-oriented journeys |

These are adjacent references, not claims that every service implements the same graph/chat product. I did not verify an open-source, publicly hosted Indian-cinema product with this exact combined feature set. That is a narrower, useful differentiator to validate rather than assume.

## Next production milestones

1. Finish the dedicated account project and verify RLS isolation, quotas and sessions against it.
2. Run the local corpus to completion, reconcile source-linked duplicates and publish measured year/role/source coverage.
3. Deploy a provenance-preserving serving snapshot; keep the local noncommercial IMDb enrichment separate.
4. Test the extension installation and the responsive graph in a browser with automation access.
5. Add permitted availability/news feeds and opt-in delivery; only then advertise live OTT recommendations or news alerts.
6. Load-test, configure budget alerts and retention jobs, complete public notices, and negotiate data/affiliate rights before monetisation.
