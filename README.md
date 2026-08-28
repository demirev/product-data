# product-data

PRNewswire press-release pipeline: scrape → label → train → classify. All services run via Docker Compose against a shared MongoDB.

Two independent questions are asked about each release, each with its own labels
and its own classifier — `launch` binary, `ai` four-way:

| task | question | classes | labels | model versions |
|---|---|---|---|---|
| `launch` | is this a new product/service launch? | `irrelevant`, `relevant` | `labels` | `distilbert-finetuned-<timestamp>` |
| `ai` | what kind of AI release is this? | `not_ai`, `ai`, `ai_feature`, `ai_tooling` | `ai_labels` | `distilbert-ai-<timestamp>` |

They are deliberately *not* a single model over both axes: the axes are
orthogonal (a release can describe an AI product without being a launch), the
label populations differ, and separate heads stay independently tunable. The
`ai` head is itself four-way because `ai_feature` and `ai_tooling` are distinct
outcomes rather than flavours of "no" — the counts worth having (how much
generative AI reaches an end market, versus how much is sold back into the AI
industry) need them apart, and collapsing them into a negative throws that away
at labelling time, where it cannot be recovered.

Index 1 is the positive class in both label spaces. That invariant is what lets
one code path report a single AUC and false-positive rate for either head, and
gate on a single `predicted_class: 1`. At serve time they compose as `launch AND
ai_class == ai`, with the AI model running only over launch-positives.

## First-time setup

```bash
docker compose build
```

Rebuild after changes to `requirements.txt` or `Dockerfile`.

### Vocabulary flags

The labeler samples on precomputed `ai_vocab` / `launch_vocab` fields rather than
scanning with a regex — a `$sample` behind a body regex over 378k articles takes
~147s, versus milliseconds on an index. New articles are stamped at scrape time;
backfill existing ones once (resumable, safe to re-run):

```bash
docker compose --profile labeling run --rm labeler python py/vocab.py
```

## Run the scrapers

Starts MongoDB plus both scrapers (link discovery + article fetching) in a continuous loop:

```bash
docker compose up -d mongo link-scraper article-scraper
```

Logs:

```bash
docker compose logs -f link-scraper article-scraper
```

### Article statuses

`pending` → `scraped` is the happy path. The two failure states differ in
whether they are ever retried:

| status | meaning |
|---|---|
| `pending` | discovered, not yet fetched |
| `scraped` | body stored, vocab flags and company facts stamped |
| `failed` | transient — DNS, timeout, 5xx, unparseable markup. Re-queued on later passes until `attempts` reaches `MAX_ATTEMPTS` (3) |
| `gone` | terminal — the release 404s. Never retried |

The distinction exists because a single blip used to be permanent: on
2026-08-19 the container's DNS stopped resolving mid-pass and, because a
resolution failure returns in milliseconds, the loop burned through **94,630**
stubs marking each one `failed` for good. `scrape_article` now fetches through a
module-level `requests.Session`, so a pass costs one DNS lookup and one TLS
handshake instead of one of each per article — the query storm that exhausted
Docker's embedded resolver is gone at the source — with transport-level retries
(3, exponential backoff) beneath that and a `(10, 30)` connect/read timeout so a
hung socket cannot stall a pass.

## Back-fill older releases

PRN's dated category listings only reach back **365 days**. Past that they don't
404 — they 301 to the undated listing and quietly serve *today's* releases, so
`scrape_prn.py --start-date 2024-05-01` would harvest today's articles and stamp
them with a 2024 `listing_date`. Never use it to reach further back than a year.

Discovery is the only thing that expires. The release pages stay live
indefinitely and `scrape_article` parses a 2011 release as happily as a 2026 one,
so back-filling is purely a matter of finding the URLs. Two scripts do that,
both writing nothing but `status="pending"` stubs that the ordinary
`article-scraper` then fills in from the live pages. Both are idempotent —
`$setOnInsert` only, so a re-run can never demote a scraped article back to
pending.

**Sitemaps first.** `robots.txt` advertises `sitemap-gz.xml`, an index of ~188
gzipped monthly files reaching back to Dec 2010 (~2.35M releases). This is the
fast path — seconds per month.

```bash
python py/scrape/scrape_sitemap.py --list                    # survey, no writes
python py/scrape/scrape_sitemap.py --from 2025-03 --to 2025-10
python py/scrape/scrape_sitemap.py --months 2024-05,2024-06
```

Roughly 70 of those monthly files are served truncated or empty by PRN,
deterministically — retrying does not help. Truncated gzip still decodes up to
the cut, so partial months are kept and flagged; `--list` prints each month
against the ~16-20k releases a healthy one carries, and the run ends by naming
the months that came up short.

**Wayback second,** for exactly those months — but as a supplement, not a
substitute, and it is much slower (several minutes per month).

```bash
python py/scrape/scrape_wayback.py --months 2025-06,2025-08,2025-09
python py/scrape/scrape_wayback.py --from 2024-06 --to 2024-07
```

Two things to know about what it returns. Its windows are scoped by **capture
date, not publication date**, so a month's query yields everything the crawler
touched that month — for Sep 2025, ~27k releases of which only ~41% are actually
Sep 2025 publications. The printed counts are therefore not month totals, and
recall for a single target month is roughly 60%. And the two sources are
complementary rather than nested: for Sep 2025 the sitemap held 647 releases
Wayback never returned, so run both. `--pad-days` (default 14) extends each
window past month end, since captures lag publication.

Broad CDX queries time out, so it walks one window at a time and halves any that
fails, down to single days. Only URLs come from Wayback; bodies are always read
from the live site.

Stubs from either script carry a `source` field (`"sitemap"` / `"wayback"`) and
deliberately no `category` or `listing_date` — neither archive exposes one, and
the authoritative `published_at` arrives with the body.

## Company data

Every release says more about its sender than the `uploader` string the scraper
keeps. The dateline names the city it was issued from, a listed company prints
its ticker, half carry an "About X" boilerplate paragraph, and three in five
link a website. None of that needs an external data source, so it is
mined from the bodies already in Mongo.

`uploader_link` (`/news/divi-resorts/`) is the company key. PR Newswire assigns
it, so it survives spelling drift in `uploader` and needs no entity resolution
of ours. There are ~78k of them across the scraped corpus.

Two layers, mirroring how `py/vocab.py` precomputes its sampling flags:

Neither layer needs running by hand in steady state: `scrape_article` stamps the
facts as it scrapes, and the article scraper rebuilds `companies` whenever its
queue empties (`--rollup-when-idle`, set on the service in `docker-compose.yml`).
The one-shot jobs exist for after an extractor change bumps a version:

```bash
docker compose --profile company run --rm company-facts                          # per-release facts
docker compose --profile company run --rm company-facts python py/company.py --rollup   # companies
```

**Per-release facts** are stamped onto each article — they belong to the
release, not the company, because a company's dateline moves over the years and
that is signal. `scrape_article` stamps new articles; the backfill catches up
existing ones and only touches articles whose `facts_version` is behind, so it
is resumable and a re-run after an extractor change re-does exactly the stale
ones. Measured extraction rates per release:

| field | rate | notes |
|---|---|---|
| `dateline_city` | 97.1% | misses are non-Latin-script releases |
| `dateline_region` | 48.4% | only about half of datelines name a state or country |
| `source_org` | 79.9% | the `SOURCE X` line, a cleaner issuer name than `uploader` |
| `domains` | 59.7% | from URLs only — PRN rewrites every e-mail to `[email protected]` |
| `about` | 53.3% | `{subject, text}` |
| `tickers` | 20.1% | `[{exchange, symbol, near_name}]` |
| `fwd_looking` | 9.5% | safe-harbour boilerplate |
| `litigation_vocab` | 7.2% | behavioural flag, see `org_type` below |
| `report_vocab` | 6.4% | behavioural flag, see `org_type` below |

Measured over the 424,766 scraped articles stamped at `tier0-1`.

**The `companies` collection** is rebuilt from those facts by `--rollup`.
Nothing in it is authored, so it can be dropped and rebuilt after any change to
the extractors; `rollup_version` records which generation produced it. It is a
full rebuild every time — it reads the corpus rather than a delta — which is why
it hangs off the scraper's idle moment rather than running per batch.

### Deciding `is_public`

Ticker ownership is decided by **concentration, not proximity**. The obvious
rule — a ticker is yours if your name sits next to it — fails outright, because
a securities firm names itself just as close to a symbol as an issuer does: The
Schall Law Firm scores 99.5% on it. What separates them is that an issuer
repeats one symbol (Veeva, `NYSE:VEEV`, in 97.8% of its releases) while a
securities firm cycles through hundreds (Pomerantz mentions 1,001 distinct
symbols and owns none).

| rule | `is_public` | confidence |
|---|---|---|
| modal ticker in >=30% of releases | `true` | high |
| safe-harbour boilerplate in >=50% of releases | `true` | medium |
| >=5 distinct tickers mentioned, modal share <10% | `false` | medium |
| >=5 releases, modal ticker share <10%, <10% safe-harbour | `false` | low |
| otherwise | `null` | — |

Note that the fourth rule keys on the modal *share*, not on whether any ticker
appears at all. HelloNation names one symbol across 2,473 releases and is
plainly not listed; a rule demanding zero mentions left it, and 15% of the
50-releases-and-up band, undecided.

The last row matters: **half of all senders have exactly one release**, and one
release is not evidence of anything. They stay `null` rather than being coded
private, which is what keeps the base rate honest. Undecided senders are
therefore concentrated almost entirely in the thin tail:

| releases per sender | senders | `is_public` null |
|---|---|---|
| 1 | 38,991 | 93.9% |
| 2-4 | 22,102 | 91.5% |
| 5-9 | 9,094 | 2.4% |
| 10-49 | 7,021 | 3.8% |
| 50+ | 987 | 4.4% |

### `org_type`

The head of the corpus by volume does not launch products. The four largest
senders are two market-research publishers (Research and Markets, Technavio) and
two securities firms (Pomerantz, the Rosen Law Firm). Names do not identify
them — "Greenberg Traurig, LLP" says nothing about law and "Persistent Systems"
is not a research house — so the rollup reads *behaviour* instead, via the two
vocabulary flags stamped on every release, aggregated to a share per company.

| rule | `org_type` | confidence |
|---|---|---|
| `.gov`/`.mil` domain, or name starting `U.S.`/`Department of` | `government` | high |
| `.edu` domain, or `University`/`College` in the name | `academic` | high |
| `litigation_vocab_share >= 0.5` | `law_firm` | high |
| `report_vocab_share >= 0.5` | `research_publisher` | high |
| >=5 distinct tickers, modal share <10% | `stock_commentary` | medium |
| `.org` domain and non-profit wording in the About | `nonprofit` | medium |
| otherwise | `operating_company` | low |

`stock_commentary` is the catch-all for senders that are neither law firms nor
research houses but still only ever write about other companies' shares —
InvestorsObserver, USA News Group.

### What is in there

78,195 senders over 424,780 scraped releases. The rollup itself takes ~27s; the
per-release backfill runs at ~16k articles/min, so a full pass over the scraped
corpus is around half an hour.

| | senders | releases |
|---|---|---|
| `is_public: true` | 6,931 (8.9%) | 19.4% |
| `is_public: false` | 13,883 (17.8%) | 56.8% |
| `is_public: null` | 57,381 (73.4%) | 23.7% |

| `org_type` | senders | releases |
|---|---|---|
| `operating_company` | 73,295 | 82.8% |
| `law_firm` | 1,668 | 6.6% |
| `research_publisher` | 656 | 5.9% |
| `nonprofit` | 1,217 | 1.9% |
| `stock_commentary` | 61 | 1.1% |
| `government` | 576 | 0.9% |
| `academic` | 722 | 0.9% |

Worth reading twice: **13.6% of the corpus comes from senders that never launch
anything** — securities firms, research publishers and stock commentators. They
are a structural negative class for the launch model, available now as a filter
rather than as something the classifier has to learn.

Coverage of the derived fields, per sender: `hq` 98.6%, `domain` 66.1%,
`about` 65.7%, `ticker` 7.4%.

```json
{
  "_id": "/news/veeva-systems/",
  "name": "Veeva Systems",
  "name_norm": "veeva systems",
  "n_releases": 313,
  "first_release": "2023-08-08T20:00:00Z",
  "last_release": "2026-08-11T14:08:00Z",
  "categories": {"health": 134, "business-technology": 79, "consumer-technology": 60},
  "ticker": {"exchange": "NYSE", "symbol": "VEEV", "share_of_releases": 0.978},
  "tickers_mentioned_distinct": 2,
  "n_releases_with_ticker": 306,
  "third_party_ticker_ratio": 0.0,
  "fwd_looking_share": 0.387,
  "litigation_vocab_share": 0.0,
  "report_vocab_share": 0.0,
  "hq": {"city": "PLEASANTON", "region": "Calif.", "share": 0.297, "method": "modal_dateline"},
  "dateline_places": [{"city": "PLEASANTON", "region": "Calif.", "n": 93},
                      {"city": "BARCELONA", "region": "Spain", "n": 44}],
  "domain": "ir.veeva.com",
  "domains": [{"domain": "ir.veeva.com", "n": 15}, {"domain": "biomarin.com", "n": 5}],
  "about": {"subject": "Veeva Systems", "as_of": "2026-08-11T11:03:00Z",
            "n_releases_with_about": 142,
            "text": "Veeva delivers the industry cloud for life sciences with ..."},
  "is_public": true, "is_public_method": "self_ticker", "is_public_confidence": "high",
  "org_type": "operating_company", "org_type_method": "default", "org_type_confidence": "low",
  "rollup_version": "tier0-1", "built_at": "2026-08-19T22:58:38Z"
}
```

### Known gaps

Translations are counted as separate releases. Veeva's 313 include the same
Barcelona event as `BARCELONA/Spain`, `BARCELONE/Espagne`, `BARCELONA/Spanien`
and `BARCELONA/España`, so per-company release counts are inflated for anyone
who distributes multilingually. The modal domain can also be a customer's
rather than the sender's, when a company names customers more often than
itself — Veeva's is `ir.veeva.com` with a customer's `biomarin.com` close
behind, and RTX's is `prattwhitney.com`. And Frost & Sullivan lands in
`stock_commentary` rather than `research_publisher`, because it discusses many
companies' shares without using enough report vocabulary to trip that rule
first.

Two gaps that used to be listed here are closed by [external
enrichment](#external-enrichment), which is the tier below. Regions were stored
as PRN prints them (`N.C.`, `Calif.`, `Espagne`) and are now normalised into
`region` against a state table and the registries' own codes. And subsidiaries
inherited their parent's listing — Toyota Motor North America was called public
off `NYSE:TM`, which is the parent's — which now resolves against SEC's ticker
list and GLEIF. The heuristic fields themselves are unchanged: `hq` still holds
the raw modal dateline, and `--rollup --no-enrichment` reproduces the old
document exactly.

## External enrichment

Everything above is mined from the release bodies, and that has a ceiling: no
founding date at all, `is_public` null for three senders in four, and `org_type`
sitting on its low-confidence default for 92,082 of 98,860. `py/enrich.py` goes
outside for the rest, resolving each rolled-up company against six public
registries and storing what each returns, whole, in `company_enrichment` keyed by
the same `uploader_link`.

```bash
export ENRICH_CONTACT="you@example.com"   # required: see below
docker compose up -d enrich                                     # continuous, resumable
docker compose run --rm enrich python py/enrich.py --refresh    # force a bulk re-download
docker compose run --rm enrich python py/enrich.py --stats      # match rates
```

**`ENRICH_CONTACT` is not optional.** SEC refuses requests whose `User-Agent`
names no contact address, so without it every SEC lookup fails and the other five
sources go out unidentified. It is deliberately not defaulted to anything — the
value is sent to all six services on every request, so it is yours to choose. The
enricher warns loudly at startup when it is unset.

| # | source | what it is for | route |
|---|---|---|---|
| I | **Wikidata** | the crosswalk hub — CIK, LEI, ISIN, parent, inception | `list=exturlusage` by domain, then `wbgetentities` |
| II | **SEC** | tickers/exchange, cik-lookup, submissions, companyfacts, Form D | bulk for the first two, per-CIK for the rest |
| III | **GLEIF** | legal entity, incorporation date, jurisdiction, parent, ISIN count | golden copy (3,409,981 records) |
| IV | **RDAP** | the domain's registration record | IANA bootstrap, then the registry |
| V | **IRS BMF** | non-profits: EIN, NTEE code, revenue, ruling date | `eo1`–`eo4` + `eo_xx` (1,959,731 rows) |
| VI | **ROR** | research institutions | Zenodo dump (135,710 records) |

Four are bulk and land in staging collections (`sec_tickers`, `sec_cik_lookup`,
`gleif_lei`, `gleif_rr`, `gleif_isin`, `irs_bmf`, `ror`) so the resolver does
indexed point lookups instead of holding 5.4M rows in RAM. The SEC's per-company
artefacts are fetched per CIK rather than from the 1.41 GB `companyfacts.zip` and
1.56 GB `submissions.zip`: only ~10k senders are registrants, so ~10k requests
beats 3 GB of download whose 99% would be discarded.

### Resumption is by version, not by time

A company is due for a source **only when that block is missing or its
`resolver_version` is behind** — the same contract `facts_version` already gives
the per-release backfill. Nothing re-fetches on a schedule, so a fresh GLEIF
golden copy refreshes *staging* without re-resolving the 98,860 companies that
have already matched. Misses are recorded rather than retried; `--retry-misses`
asks again when you want a second look, and a transient error is not a miss, so
it is re-queued by the ordinary check on the next pass.

Each source is wrapped independently — a GLEIF timeout does not cost the RDAP
record that came back fine. Companies are resolved biggest sender first, which is
what makes the pass interruptible: a full sweep runs about **3.6 s/company** (the
RDAP and Wikidata rate limits dominate), so ~4 days end to end, but the head of
the corpus lands in the first few hours.

### Matching

Wikidata runs first because one match hands over P5531 (CIK), P1278 (LEI) and
P946 (ISIN), turning three downstream fuzzy matches into exact-key lookups.

| source | 1st | 2nd | last resort |
|---|---|---|---|
| Wikidata | `exturlusage(domain)`, verified against P856 | — | — |
| SEC | Wikidata P5531 | ticker **and** filer name | `name_norm` in cik-lookup |
| GLEIF | Wikidata P1278 | `name_norm` + country | `name_norm` worldwide |
| IRS | EIN from SEC submissions | `name_norm` + state | `name_norm` nationwide |
| ROR | Wikidata QID | domain | `name_norm` (aliases included) |
| RDAP | domain | — | — |

Two rules do most of the work. **A name block has to come back with exactly one
row**, so widening a block can lose a match but never invent one — that is why
the last-resort tiers are safe, and `matched_by` records which tier answered so
they can be dropped wholesale.

**And the ticker path is corroborated by name.** `companies.ticker` is the
rollup's concentration guess, and for a subsidiary that guess is the parent's
symbol. Following it blindly would look up Toyota Motor Corp's CIK for Toyota
Motor North America and launder a known-bad inference into an authoritative
identifier. Instead the mismatch is *recorded* — and it turns out to be exactly
the evidence that closes the gap below.

`exturlusage` needs the verification pass because it indexes every item that
*cites* a domain, not the one that owns it: `sandiegozoo.org` returns six items,
of which four merely reference the site.

### What it finds

Measured over the first 506 companies resolved — which, because the pass runs
biggest sender first, is the head of the corpus and not a random sample. Expect
the tail to be far thinner: a sender with one release is usually a company no
registry has ever heard of.

| source | matched | how |
|---|---|---|
| RDAP | 95.1% | domain 481 |
| Wikidata | 60.1% | P856 domain 304 |
| GLEIF | 43.5% | Wikidata P1278 153, name+country 52, name 13 |
| SEC | 43.1% | Wikidata P5531 120, ticker+name 46, cik-lookup 40, tickers list 12 |
| ROR | 34.8% | Wikidata QID 119, domain 44, name 13 |
| IRS BMF | 3.6% | name 14, name+state 3, SEC EIN 1 |

Which becomes, on `companies`:

| field | coverage | field | coverage |
|---|---|---|---|
| `domain_registered_year` | 95.1% | `sic` | 36.6% |
| `country` / `region` / `hq_city` | 72.3% | `ror_id` | 34.8% |
| `founded_year` | 70.2% | `assets` | 34.0% |
| `wikidata_id` | 60.1% | `revenue` | 33.4% |
| `legal_form` | 54.2% | `employees` | 16.8% |
| `domicile` | 50.0% | `parent_wikidata_id` | 16.6% |
| `is_public` (external) | 48.6% | `parent_lei` | 4.3% |
| `lei` / `cik` | 43.5% / 43.1% | `ein` / `ntee_code` | 3.6% / 3.0% |

`founded_year` at 70% is the headline: the rollup had no founding date at all,
and 60% of those years come from Wikidata P571 with the rest spread across
GLEIF's incorporation date, ROR, Form D, the IRS ruling month and SEC's first
filing.

### What crosses over to `companies`

`--rollup` folds a curated subset onto each company. It has to happen inside
`rollup` — that writes with `ReplaceOne`, so a separate pass would be overwritten
by the next rebuild — which also means the article scraper's `--rollup-when-idle`
picks the enrichment up with no extra step.

| field | precedence |
|---|---|
| `wikidata_id` `cik` `lei` `ein` `ror_id` | the join keys, for whatever comes next |
| `founded_year` + `_src` | Wikidata P571 › GLEIF creation date › ROR established › Form D › IRS ruling › SEC first filing |
| `domain_registered_year` | RDAP |
| `country` `region` `hq_city` + `location_src` | SEC (US only) › GLEIF › Wikidata › ROR › IRS › dateline |
| `domicile` + `_src` | SEC state of incorporation › GLEIF jurisdiction › Form D › Wikidata P740 |
| `sic` `sic_desc` `ntee_code` `legal_form` | SEC, IRS, GLEIF respectively |
| `is_public` + `_method` + `_confidence`, `ticker`, `listings` | see below |
| `revenue` `revenue_unit` `revenue_year`, `assets`, `employees` | SEC XBRL › IRS BMF; employees from Wikidata P1128 |
| `parent_wikidata_id` `parent_lei` `parent_name` | Wikidata P749/P127, GLEIF level 2 |
| `enrichment_sources` `enriched_at` `enrichment_version` | which sources answered |

**Direct statements beat proxies for age.** GLEIF has Smithfield incorporating in
1997 and the SEC first hearing from it in 1994; the company was founded in 1936
and only Wikidata says so. The IRS ruling month and the first filing date are
last precisely because they date a registration rather than a founding.

**Money carries its unit.** Toyota reports 29.9 trillion — JPY. A bare number
would be three orders of magnitude wrong the moment anyone compared it.

**SEC addresses are only used when they are US.** EDGAR codes foreign locations
in a scheme of its own (`M0` for Japan) and files the country in the city line,
so a foreign filer would otherwise contribute country `M0`, city `JAPAN`. GLEIF
and Wikidata state a real ISO country and answer for those.

### `is_public`, revisited

The heuristic reads concentration of ticker mentions, which is the best a press
release can support. A registry does better in both directions:

| rule | `is_public` | confidence |
|---|---|---|
| a symbol in SEC's own ticker/exchange list | `true` | high |
| Wikidata P414 stock exchange | `true` | medium |
| modal ticker belongs to a differently-named filer, and another registry knows this company | `false` | medium |
| GLEIF knows the entity and maps zero ISINs | `false` | medium |
| otherwise | the heuristic's answer, untouched | |

The GLEIF rule is withheld when the heuristic already found a concentrated
self-ticker: GLEIF maps ISINs it has been told about, and silence there is weaker
evidence than a symbol repeated across a company's own releases.

It corrects in both directions. Smithfield Foods came out of the rollup as
`is_public: false` via `no_consistent_ticker` — its releases simply do not print
`Nasdaq: SFD` often enough — and SEC's list settles it as `true` at high
confidence. Run `--rollup --no-enrichment` and it reverts, which is how you
measure that.

The third rule is the **known gap from the previous section, closed**. Toyota
Motor North America prints `NYSE:TM` in 63.8% of its releases and was called
public off it. SEC's list says `TM` belongs to `TOYOTA MOTOR CORP/`; GLEIF knows
`TOYOTA MOTOR NORTH AMERICA, INC.` as its own entity in Plano, TX. So the ticker
is the parent's, and the subsidiary is now `is_public: false`,
`ticker_belongs_to_other_filer` — with the SEC name path having meanwhile found
TMNA's *own* CIK, `0002109822`.

`org_type` is treated more conservatively: an external answer is only allowed
through when the heuristic fell back to its `operating_company` default. Nothing
external says "this is a securities firm trawling for plaintiffs" or "this is a
market-research publisher", and those behavioural classifications are the ones
that matter most here — a registry calling Pomerantz an operating company is true
and useless.

Re-running `python py/company.py --rollup --no-enrichment` rebuilds the
heuristic-only document, which is what makes every overwrite above reversible and
lets you measure how often the rules were right on their own.

### Worked example

```json
{
  "_id": "/news/smithfield-foods%2C-inc./",
  "name": "Smithfield Foods, Inc.",
  "wikidata_id": "Q1477575", "cik": "0000091388",
  "lei": "CVTIPZRZJC0JQEZLL598",
  "founded_year": 1936, "founded_year_src": "wikidata_p571",
  "domain_registered_year": 1997,
  "country": "US", "region": "VA", "hq_city": "Smithfield", "location_src": "sec",
  "domicile": "US-VA", "domicile_src": "sec_state_of_inc",
  "sic": "2011", "sic_desc": "Meat Packing Plants",
  "legal_form": "HOV4", "legal_form_src": "gleif_elf",
  "is_public": true, "is_public_method": "sec_ticker_exchange",
  "is_public_confidence": "high",
  "ticker": {"exchange": "Nasdaq", "symbol": "SFD", "source": "sec"},
  "revenue": 15531000000, "revenue_unit": "USD", "revenue_year": 2025,
  "assets": 12177000000, "assets_unit": "USD",
  "parent_wikidata_id": "Q17499655", "parent_name": "WH Group",
  "parent_lei": "254900A4AYEDRWYSE928",
  "parent_relation": "IS_ULTIMATELY_CONSOLIDATED_BY",
  "enrichment_sources": ["wikidata", "sec", "gleif", "rdap"],
  "enrichment_version": "tier1-1"
}
```

Note the founding year: the rollup had no such field at all, GLEIF says 1997 and
the SEC says 1994, and the answer is 1936.

### Known gaps

**Private companies are hard, and that is the point of the last-resort tiers.**
Kentik has a Wikidata item with no CIK on it, and EDGAR files it as `KENTIK
TECHNOLOGIES, INC.`, which does not normalise to `kentik` — so its four Form D
filings, which state its founding year and revenue band, are not reached. Exact
name blocks are what keep the false-positive rate at zero, and this is the price.

**ROR lags renames.** The corpus's `San Diego Zoo Wildlife Alliance` is in ROR as
`Zoological Society of San Diego` with the alias `San Diego Zoo Global`, on the
domain `sandiegozoo.org` rather than `sandiegozoowildlifealliance.org` — so ROR
misses it, though the IRS BMF finds it by name and supplies EIN, NTEE code and
$437.6M of revenue.

**Country is the binding constraint on GLEIF and the IRS.** Both are matched on a
name block cut by region where one is known, and fewer than half of datelines
name one.

## Label articles

Web UI for building the `labels` and `ai_labels` collections. Profile-gated so it
doesn't run by default.

```bash
docker compose --profile labeling up -d labeler
# http://localhost:8000     product-launch labeler
# http://localhost:8000/ai  AI-product labeler
```

Set the labeler name by editing `LABELER_NAME` in `docker-compose.yml`.

Both labelers work the same way: a sampled article, a rubric, a decision, and
optional metadata. What they record differs. Launch is binary (`is_relevant`)
plus one *flagged hard negative* — **irrelevant w/ product info**
(`has_product_details`), trained as a negative but retrievable as a group. AI is
four-way, written as a single `ai_class` that is literally the class index the
model trains on:

| `ai_class` | decision | what it means |
|---|---|---|
| `0` | not AI | there is a product, but nothing generative is central to it or being shipped |
| `1` | AI product | generative AI is the value proposition, sold into an end market |
| `2` | AI feature | a largely non-AI product bolting a generative feature onto itself |
| `3` | AI tooling | the buyer is the AI industry itself — RAG and vector databases, agent frameworks, inference and compute providers, eval tooling |

What differs otherwise is the question, the sampling, and the drop folder for LLM
batches.

- **Launch** oversamples launch-keyword headers 70/30 against an unbiased random
  stream.
- **AI** serves articles that already carry a launch label first (they match what
  the AI model sees at serve time), then falls back to a tiered draw on
  `ai_vocab`: 50% generative-AI vocabulary, 25% generic AI/ML wording — the
  classic-ML and AI-washing hard negatives — and 25% unrestricted. It has an extra
  **no product / N/A** button, because "is the product an AI product?"
  presupposes a product; those land in `ai_label_skips` with
  `reason="no_product"` instead of polluting the training set with negatives.

The AI card also offers an optional **launch** dropdown when the article has no
launch label yet — you're reading the article anyway. Those are written with
`source="ai_side"` and never overwrite an existing label; they carry the AI
sampler's `sample_method`, so they can be filtered out of base-rate estimates
(they come from an AI-biased sample).

### Review LLM-suggested labels

Drop CSVs of provisional labels into `data/labels-for-review/` (launch) or
`data/ai-labels-for-review/` (AI) — both git-ignored, with the schema documented
in each folder's `README.md` — then open the labeler and click **review mode**
(http://localhost:8000/review/launch, http://localhost:8000/review/ai). Pick a
file and verify each row: the decision form arrives pre-filled from the CSV, so
submitting unchanged confirms the LLM's call and changing a radio overwrites it.
Progress resumes automatically and each verified row lands in the task's label
collection tagged `source="llm_reviewed"` with the original `llm_label` and a
`human_agreed` flag, so you can later score the LLM and mine its mistakes.

Each folder's README is a self-contained brief — point an LLM at it and it has
everything needed to produce a review CSV:
[launch](data/labels-for-review/README.md) ·
[AI](data/ai-labels-for-review/README.md).

## Train the classifiers

One-shot jobs. Read the task's labels + `articles` from Mongo and write a
checkpoint to `<models>/<task>/<version>`. Requires network access (downloads
`distilbert-base-uncased`; the container is `--rm`, so nothing caches between
runs).

```bash
docker compose --profile training run --rm train      # launch  -> /models/launch/<version>
docker compose --profile training run --rm ai-train   # ai      -> /models/ai/<version>
```

Optional flags (append after the service name):

```bash
docker compose --profile training run --rm ai-train \
  python py/classify/finetune.py --task ai --epochs 5 --batch-size 8
```

Every run is from scratch off `distilbert-base-uncased` — there is no incremental
training. Re-run whenever you have added labels.

### Runs and versions

A run is named for the moment it started, in UTC:
`distilbert-finetuned-20260819232201`. That one string is the directory it lives
in, the `model_version` its predictions carry, and its `_id` in the `model_runs`
collection, so any prediction traces back to the weights and the eval numbers
that produced it. Runs accumulate side by side; nothing is overwritten.

```
/models/launch/distilbert-finetuned-20260819232201/
├── config.json, model.safetensors, tokenizer*   # the artefact inference loads
├── metrics.json                                 # accuracy, ROC-AUC, confusion matrix,
│                                                #   per-class report, sizes, label counts
└── eval_predictions.csv                         # per-example true/predicted class + p(class)
```

The Trainer's per-epoch checkpoints carry optimiser state and run several hundred
MB each; they exist only so the best epoch can be restored at the end, so they go
to a scratch directory that is deleted when training returns. Only the final
artefact is kept.

`metrics.json` is also written to Mongo as a `model_runs` document, which is how
you compare runs without going to the volume:

```javascript
db.model_runs.find({task: "launch"}, {_id: 1, trained_at: 1, n_train: 1,
                                      "metrics.eval_accuracy": 1, "metrics.eval_roc_auc": 1})
             .sort({trained_at: -1})
```

Headline `roc_auc`, `false_positive_rate` and `false_negative_rate` are
one-vs-rest against class 1 (`relevant` / `ai`) on both tasks, so a four-way run
stays comparable with a binary one and with its own earlier runs; the rest of the
four-way picture is in `metrics.confusion_matrix` and the per-class report, which
is where you see *which* class a missed AI product was read as.

Two caveats when comparing. The 80/20 split is stratified with a fixed seed, but
it is drawn over whatever labels exist at the time, so consecutive runs hold out
different articles — the numbers track a moving evaluation set, not a fixed one.
And stratification is dropped for a run where some class has fewer than two
labelled examples, which a four-way split hits easily early on; the run prints a
line when that happens.

## Run inference

Continuous services. Each waits for a checkpoint, then classifies articles that
don't yet have a prediction for the version it is serving, looping forever.

```bash
docker compose up -d classifier      # all scraped articles
docker compose up -d ai-classifier   # only articles the launch model called relevant
```

**Which model gets served.** Unpinned, each pass re-resolves the newest run under
`/models/<task>` — so a retrain is picked up on the next pass with no restart, and
its predictions land under the new version. Set `MODEL_VERSION` in
`docker-compose.yml` to pin one run instead.

That default has a cost worth knowing: a new version has no predictions, so
picking one up means re-scoring the corpus from zero — 3M+ articles for `launch`.
If you would rather retrain freely and switch deliberately, pin `MODEL_VERSION`
and move it when you mean to.

`ai-classifier` passes `--require-relevant-from launch`, which tracks the newest
*launch* run; pass a version string instead to pin the gate. Either way it stays
idle until that model has produced positives.

Each prediction carries `predicted_class` — an index into the `classes` recorded
on that run's `model_runs` document — and `class_probabilities`, the whole
distribution rather than one positive-class score, so a threshold can be re-tuned
without a second pass over the corpus. The gate matches `predicted_class: 1`.

Predictions are keyed by `(article_id, model_version)`, so every model and every
generation of it writes to the same `predictions` collection without colliding.
Both the resume filter and the gate are `$lookup` joins on that unique index
rather than id lists assembled in Python — with 3M+ articles whose `_id`s are URL
slugs, a `$nin` of everything already classified outgrows Mongo's 16MB query
limit as soon as one full pass completes.

## Stop everything

```bash
docker compose down
```

Add `-v` to also drop the `mongo_data` and `models` volumes.
