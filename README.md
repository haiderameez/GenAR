# GenAR: evidence-grounded regulatory report generation

A system that takes a reporting task, gathers the evidence it needs, reasons over
it, and produces a controlled, traceable document. PADER is the first report type
it knows about, not what it is. `reports/psur_mini.yaml` is a second one, and it
required no Python.

The design premise, in one line: **Python decides what is true. The model decides
what is worth saying about it. A deterministic checker decides whether it said it
honestly.**

---

## 1. Running it

The whole system is one command. Point it at a spreadsheet, name a report type,
get a verified report.

```
data in  →  python run.py run --report pader --data <file>  →  output/report_output.md
```

`--data` accepts `.xlsx` or `.csv`. The loader picks by extension and both
produce identical figures.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
```

Copy `.env.example` to `.env` and fill it in with the dataset path and, if you
want the model-written version, a Gemini key. The dataset is not in this
repository, because the data usage notice asks that it not be redistributed.

```bash
cp .env.example .env
```

```ini
GENAR_DATASET=C:/Users/you/Downloads/Bisoprolol_icsr_sample_1068rows.xlsx
GEMINI_API_KEY=...            # only needed for --llm gemini
```

No quotes and no spaces around `=`, though quotes are accepted and stripped if
you use them. `.env` is gitignored and excluded from the submission. Anything
already set in your shell wins over it. Every setting can equally be passed as a
flag (`--data`, `--model`) or exported directly.

`run.py` is a two-line launcher that puts `src/` on the import path, so there is
no install step. `python -m genar.cli` works identically if `src` is on
`PYTHONPATH`.

One command regenerates the report, once `GEMINI_API_KEY` is set:

```bash
python run.py run --report pader
```

Other things worth knowing:

```bash
python run.py run --report psur_mini      # the second report type
python run.py analyze --report pader      # stop at the first review gate
python run.py run --sections trends       # regenerate one section: one call
python run.py run --gate strict           # refuse to render without approval
python run.py list-facts                  # every analysis available
pytest -q                                 # 100 tests
```

Output lands in `output/report_output.md`, review files in `review/`.

---

## 2. What it produced

From 1,068 rows, over the period **2024-12-27 to 2025-12-26**:

| | |
| --- | --- |
| Cases | 1,024 (from 1,068 rows, 44 of which were superseded follow-up versions) |
| Serious / non-serious | 1,023 / 1 |
| Expedited (15-day Alert) | 1,023 |
| Fatal outcome reported | 68 cases |
| Reaction events | 3,423 (deduplicated) · 3,642 (all rows) |
| Distinct preferred terms | 1,121 |
| Most reported reaction | Acute kidney injury, 80 cases |

Four of the nine sections are rendered without a model at all. Five call it once
each.

| Run | Model | Sections | Claims checked | Grounded | Violations |
| --- | --- | --- | --- | --- | --- |
| Delivered report | `gemini-3.6-flash` | 5 | 305 | 305 | 0 |

An excerpt of what the model produced, from Trends. Every figure traces to a
declared fact, each movement is stated with both counts and both periods, and
none of them is explained:

> Acute kidney injury was the most frequently reported term in both halves of the
> reporting period, though cases reporting Acute kidney injury fell from 48 cases
> (9.4%) in the first half of the period (511 total cases) to 32 cases (6.2%) in
> the second half (513 total cases).

---

## 3. Architecture

![Architecture Diagram](Architecture%20Diagram.png)


Twelve modules, flat, no framework:

| File | Responsibility |
| --- | --- |
| `loader.py` | Read xlsx/csv, normalise, deduplicate to latest case version |
| `validate.py` | 17 data-quality checks, two of which are hard invariants that abort the run |
| `facts.py` | `Fact` and `FactStore`, the unit of evidence and its invariants |
| `analyses.py` | The analysis registry and every computation |
| `spec.py` | Load and validate a report type from YAML |
| `packet.py` | Assemble the scoped context for one section |
| `llm.py` | The Gemini client, with budget, throttle and retry |
| `generate.py` | Packet in, section text out. Deliberately thin |
| `verify.py` | The grounding checks |
| `review.py` | The two human gates |
| `render.py` | Deterministic sections, tables, appendices, document assembly |
| `errors.py` | The typed exception taxonomy every module raises from |

There is no agent, no orchestration framework and no retrieval layer. The
pipeline is a fixed sequence with a fixed dependency order, which a `for` loop
expresses exactly. An agent would add the ability to choose its own path, and the
whole design intent here is that it must not have one. Retrieval would add
approximate lookup over evidence that is already exact and addressable by id.

---

## 4. Where AI is used, and where it is not

**The model writes five sections of prose.** That is all it does.

It never sees the dataset. It never sees a row. It sees a list of labelled
figures and is asked to select among them and phrase them. Everything else is
Python: parsing, deduplication, counting, ranking, percentages, tables, the
header block, the statements of unavailable data, the case listing, and every
check afterwards.

| Question | Answer |
| --- | --- |
| How many serious cases? | Python. `count(distinct safetyreportid where serious == 'serious')` |
| What share is that? | Python. One rounding rule, `facts.format_percent` |
| Which reactions matter enough to name here? | **Model** |
| Which of these movements is worth surfacing to a reviewer? | **Model** |
| How should this be worded for a regulator? | **Model** |
| Is what it wrote supported? | Python |
| Can it be published? | **Human** |

The line is that *counting is not a language task, and phrasing is not an
arithmetic task*. Every place the model touches a number, it is placing one it
was handed, never producing one. That claim is enforced rather than trusted. See
§6.

Four sections were deliberately taken away from the model:

- **Reporting Period.** A table of eight values. Prose adds nothing.
- **History of Actions.** A statement that nothing was supplied. This is the
  section most exposed to invention, because a model asked to write about safety
  actions has a great deal of plausible material available and no data at all.
- **Methods and Data Quality.** The validation findings, rendered as they are.
- **Case Index.** A line listing.

Every table in the document, including tables under model-written prose, is
rendered from facts. The prose describes the shape of a distribution. The table
beneath it carries the figures.

---

## 5. The prompts

Three layers, split by what varies.

### 5a. Invariant rules, in [`prompts/system.md`](prompts/system.md)

One file, identical for every section and every report type. It carries only what
is true regardless of what is being written. Numbers come from the supplied list
and nowhere else. A figure labelled *cases* must be written as cases. An
unavailable figure is stated as unavailable and never as zero. Report what was
observed and do not explain it. Neutral regulatory register, no headings, no
preamble.

The grain rule earns its place in the system prompt because of *why* it fails.
`"80 reactions"` and `"80 cases"` are equally fluent English and only one is
true. A wrong number looks wrong. A right number on the wrong noun does not.

### 5b. Per-section brief, in the report YAML

Never in code, never in the system prompt. Each section carries the evidence it
needs, what to do with it, and how far it may go:

```yaml
- id: trends
  heading: Trends and Important Observations
  claim_level: derived
  max_words: 320
  requires: [period.label, cases.by_month, cases.serious_by_month,
             cases.top_reactions_first_half, cases.top_reactions_second_half]
  tables:   [cases.by_month, cases.top_reactions_first_half,
             cases.top_reactions_second_half]
  instructions: |
    Surface the movements in the reported data that a reviewer should look at.
    ...
    Phrase every movement as a statement of what was reported, with both
    figures and both periods present. "Cases reporting X fell from 48 in the
    first half of the period to 32 in the second" is the form required.

    A change in reporting frequency is not evidence about the product. Do not
    explain a movement, do not attribute it to a cause, do not call it an
    increase in risk, and do not characterise any of it as a signal or as
    reassurance. Report the movement and stop.
  closing_note: |
    The observations above are reported movements in case counts. They have not
    been assessed for clinical significance, and are presented for evaluation by
    a qualified reviewer.
```

`requires` is the entire context-engineering decision, expressed as data. A
section that does not declare a figure cannot mention it, because the figure is
not in its prompt. `closing_note` is appended verbatim, because the caveat that
matters most is the one thing least safe to ask a model to remember.

**Claim levels** encode the Starter Guide's observed, derived and interpretation
ladder as configuration:

| Level | Permits | Granted to |
| --- | --- | --- |
| `observed` | restating supplied figures | no section |
| `derived` | rankings, comparisons, movements *present in the packet* | all five model sections |
| `interpretation` | assessing significance | no section, in Version 0 |

Trends gets `derived` deliberately. A trends section that may only restate totals
is useless, because the job is to surface movements. What it may not do is
convert a movement into a conclusion, and that is what the verifier enforces.

### 5c. Assembled evidence, built per section at run time

Real output, `narrative_summary`, abridged:

```
Report: Periodic Adverse Drug Experience Report (PADER) for Bisoprolol
Section to write: Narrative Summary and Analysis

APPROVED FIGURES
These are the only figures you may state. Each is written here in the form you must use.

[cases.total] Total cases: 1,024 cases
    counted as: cases
    basis: count of distinct safetyreportid, highest version retained

[cases.serious] Serious cases: 1,023 cases (99.9% of 1,024)
    counted as: cases
    basis: count of cases where serious == 'serious'

[cases.top_reactions] Most frequently reported reactions, by number of cases (top 10) (of 1,024 cases):
    Acute kidney injury: 80 cases (7.8%)
    Drug ineffective: 54 cases (5.3%)
    ...
    counted as: cases
    basis: count of distinct cases reporting each MedDRA preferred term

[events.total] Reaction events reported: 3,423 reaction events
    counted as: reaction events, latest version of each case only
    basis: sum of reaction terms across cases, latest version of each case only

[absent.expectedness] Classification of reactions as labelled or unlabelled: not available from the supplied dataset
    reason: determining expectedness requires the approved product label or CCDS, which was not supplied

The items marked as not provided or not available above must be stated as
unavailable, using the reason given. None of them is zero.

WHAT THIS SECTION MUST DO
[the section's instructions]

CLAIM LEVEL: derived
[the rule for that level]

Length: at most 320 words.
```

Three deliberate choices in that packet:

- **`counted as:` on every figure.** The population is carried with the number,
  not left to the label.
- **`basis:` on every figure.** The model is shown the rule, so an ambiguous
  figure can be described accurately rather than guessed at.
- **Absent evidence is present.** `absent.expectedness` is *in* the packet as a
  fact with a reason. Omitting it would leave silence, and silence in a safety
  report reads as an absence of findings.

Percentages are in the packet because they were computed, not so the model can
copy a style. It has no way to produce one that was not supplied, because the
verifier rejects any percentage that is not a rendering of a supplied fact.

---

## 6. How it stays grounded

Four mechanisms, in order of how much they are relied on.

**1. The model cannot reach ungrounded data.** It receives a list of facts, not
the dataset. Most hallucination risk is removed by not creating the opportunity.

**2. Every claim is checked afterwards, deterministically.** `verify.py` builds
the set of surface forms the section's own packet licenses, working from typed
`Fact` values rather than by scraping digits, then reads the generated text and
matches every number and date against it. `cases.serious = 1023` with denominator
`cases.total = 1024` licenses `1023`, `1,023` and `99.9%`, all tagged case-grain.
Anything unmatched fails the section, and the failure names the sentence.

A test asserts the correspondence directly. *Every number the packet showed the
model must be accepted by the verifier.* Without it, the two could drift and
every other guarantee would be hollow.

**3. Grain attribution.** For each matched number, the verifier reads the first
counting noun after it. A case-grain figure written as *reactions*, *events* or
*terms* fails, and the reverse fails too. When a value is licensed by facts of
both grains there is nothing to attribute, so the check stays silent rather than
guessing.

**4. Claim-type checks.** A phrase list blocks conclusions no section of this
report can support, covering causality, signal status, "no safety concerns",
"consistent with the known safety profile" and "disproportionately affected".
Separately, if a section's packet contains an unavailable fact, the text must say
so, and it must not report that concept as zero or none.

None of this uses a model. A model checking a model is not a control.

**It has caught the model in the wild, not just in tests.** Regenerating the
report against a live model at temperature 0, the Reaction Analysis section came
back with:

> ...with the case count for drug ineffective decreasing by **1** case in serious
> cases while the case counts for all other top ten terms remained unchanged.

54 all cases minus 53 serious cases is 1. The model did the subtraction, which is
the one thing the system prompt forbids outright, and the figure 1 appears
nowhere in that section's evidence. The verifier failed the section and named the
sentence. Nothing about that output reads as wrong. It is fluent, plausible and
arithmetically correct. It is simply a number the pipeline cannot vouch for, and
a number the pipeline cannot vouch for does not belong in a regulatory document.

This is also why re-running can surface a violation the previous run did not.
Temperature 0 pins sampling, not model behaviour across every prompt. The
delivered report carries zero violations. A regeneration is not guaranteed to,
and that is the control working rather than failing.

**A check that punishes the right answer is worse than no check.** The first
Gemini run flagged two violations in *Serious Cases*, and both were the
verifier's fault. The model had written *"recorded as unavailable rather than
zero, to avoid asserting that no earlier cases exist"*, which is exactly the
behaviour the system prompt asks for, and a bare `\bzero\b` search flagged it for
containing the word. The check now looks for zero being *asserted* as a value,
and ignores it being named in order to be rejected. Both the true positives and
that false positive are now regression tests.

**Proof that the checks fire.** Negative tests each break a passing section in
one specific way:

| Injected | Caught as |
| --- | --- |
| `1,025 cases` | `ungrounded_number` |
| `1,024 reaction events` | `grain_mismatch` |
| `12.4% of the case series` | `ungrounded_number`, a share nobody supplied |
| `No safety concerns were identified` | `banned_phrase` |
| `consistent with the known safety profile` | `banned_phrase` |
| `Elderly patients were disproportionately affected` | `banned_phrase` |
| silence about an unavailable figure | `missing_absence_statement` |
| `Zero labelling changes were recorded` | `absence_reported_as_zero` |
| `21 CFR 600.80` | `unsupported_citation` |

And the control: `1,024 cases` in the same position passes.

**What a reader sees.** Appendix A of the report lists every fact it rests on
with its value, its computation rule and how many cases it was computed from.
Appendix B gives the per-section grounding score. Appendix C is the run manifest,
holding the dataset hash, config file, system-prompt hash, model id, call count
and review mode.

---

## 7. Human control

Two gates, both plain JSON files edited by hand.

**Gate 1, between analysis and generation.** `review/pader_analysis_review.json`
lists every computed figure with its value, grain, method, the number of cases
behind it and a sample of their ids. A reviewer sets `approved` or `flagged`. A
flagged figure is withheld from every section that declared it, and those
sections are not generated. The document says so where they would have been.

**Gate 2, between generation and rendering.** `review/pader_section_review.json`
holds each generated section with its verification record. Flagged sections are
withheld.

`--gate advisory` (default) blocks flagged items and lets pending ones through
with the document marked **REVIEW PENDING** on its first page. `--gate strict`
refuses to render at all until everything is approved. Approval survives re-runs,
but not changes. If a figure's value changes, its decision resets to pending and
the previous value is recorded, so an approval cannot silently carry over to
something else.

JSON rather than a UI is a scope decision, not a design one. A real deployment
needs reviewer identity, timestamps, an audit trail, per-figure comment threads
and an e-signature step under 21 CFR Part 11. The mechanism that matters is real
here: a flagged figure cannot reach a section, and an unapproved document is
marked as such.

---

## 8. Running on a free tier

A full report is five model calls. Free-tier limits are per-minute and per-day
and are shared with everything else on the key, so the pipeline never assumes a
burst will get through:

- **No response cache.** Every model-written section is a live call, every run.
  The pipeline never replays a stored answer, so what you see generated is what
  the model produced just now.
- **`--sections trends`** regenerates named sections only, reusing the rest from
  the previous run's review file. Fixing one prompt costs one call, not five.
- **Daily budget, counted per model**, persisted across restarts, refusing the
  call that would exceed it rather than collecting 429s halfway through a
  document. Provider quotas are per model, so the counter is too.
- **Client-side throttle**, a minimum interval derived from `GENAR_RPM`, enforced
  inside the client so no code path can go around it.
- **Retry with exponential backoff and jitter** on 429 and 503, honouring a
  server-supplied `retryDelay` when present.
- **Sequential generation.** No concurrency anywhere.

**The real free-tier limit, measured rather than assumed, is 20 requests per day
per model.** Not per key, per model. At five calls per report that is four full
reports a day on any one model, and switching `--model` gives a fresh allowance.
`GENAR_DAILY_BUDGET` now defaults to 20 to match. `GENAR_RPM` defaults to 10.
Model id is `GENAR_MODEL` or `--model`, and temperature is 0.

**A daily-quota 429 is not retried.** Finding the 20/day limit the hard way also
exposed a bug. The retry loop treated every 429 as transient and spent 463
seconds backing off against a quota that resets tomorrow. Per-minute limits and
503s are still retried. A daily exhaustion now fails immediately, names the
model, and points at `--model`.

**Model pinning.** `gemini-3.6-flash`, pinned to a version rather than an alias
like `gemini-flash-latest`, because a report has to be reproducible and an alias
silently changes what produced it. Availability moves fast. `gemini-2.5-flash`
was already closed to new keys when this was built, which is why the default is
a setting and not a constant.

**Three things the real API taught this code:**

1. **Reasoning tokens come out of `max_output_tokens`.** The first Gemini run
   returned 29-word sections. The budget was being spent thinking, and sections
   were severed mid-figure (`1 cases (0.1`). Fixed by raising the limit to 8,192
   and setting `thinking_level=MINIMAL`, since this task is restatement under
   explicit instructions, not a problem to reason through.
2. **A truncated section must fail, not pass.** `finish_reason` is now checked
   and `MAX_TOKENS`, `SAFETY`, `RECITATION` and friends raise. Truncated
   regulatory prose reads as well-formed right up to where it stops, which makes
   it the worst possible thing to accept quietly. Notably the *verifier* caught
   the damage on its own, because the severed `0.1` failed as an ungrounded
   number, but a downstream check should not be the thing standing between a
   truncated response and a document.
3. **Free-tier 503s outlast a short backoff.** Retries went from 4 attempts to 6,
   at 2s doubling to a 45s cap, honouring a server-supplied `retryDelay`.

---

## 9. What the data turned out to be

Four things in this dataset change reported numbers, and three of them are not
mentioned in the supplied guides. Each is surfaced in the report's Methods
section rather than silently handled.

### 1,068 rows are 1,024 cases, and the surplus is not what the guide says

The Starter Guide describes the extra rows as cases with more than one reaction
row. They are not. They are **follow-up versions** of an existing case. Within a
repeated `safetyreportid` the `safetyreportversion` (1 through 8), receipt date,
company number and reaction list all differ. The rule applied is to keep the
highest version per case. Counting them as separate cases would inflate the case
count, and counting their reactions would inflate the event count with superseded
information.

### Some MedDRA preferred terms contain commas

`patient_reaction_reactionmeddrapt` is comma-packed, but three of the terms in
this file are themselves comma-separated: `Hallucination, visual`,
`Hallucination, auditory`, `Hallucinations, mixed`. A naive split produces
phantom reactions named `visual`, `auditory` and `mixed`.

MedDRA terms are sentence-cased, so a token beginning with a lowercase letter is
a continuation. Across all 1,068 rows exactly three such tokens occur, all
genuine continuations. Applying the rule makes the reaction, outcome and
MedDRA-version lists align on **every** row. That is an invariant the validator
asserts, and the thing that makes positional outcome pairing correct rather than
best-effort. Before the repair, six rows had more reaction terms than outcomes,
which would have silently mis-assigned outcomes from that point on in each row.

This is also where our figure differs from the reference report supplied with the
exercise. It reports **3,648** reaction events across all rows. The correct
figure is **3,642**. The difference is exactly those six terms, each split into
one token too many.

### Two counts of the same reaction, both correct

The challenge brief's worked example gives `Acute kidney injury: 22`, which is
what whole-cell counting produces. The reference report says 80 cases. Ours says
80, after deduping versions, splitting terms properly and counting distinct
cases.

For *Drug ineffective* we get **54** where the reference says 53. The cause is
exact. The reference's case presentation covers the serious population, and the
single non-serious case in the entire dataset (id `25503311`, United States) is
one of the 54. Both numbers are right, because they answer different questions.
The report carries both, as `cases.top_reactions` and
`cases.top_serious_reactions`, which the brief asks for anyway.

### Age unit 800 means decades

Three cases arrive with `patient_patientonsetageunit = 800.0`. That is the ICH
E2B code list, where 800 is *decade*, not the number 800 and not years. Read as
years those patients are 7, 9 and 3 years old. They are 70, 90 and 30. Nine more
cases arrive in months, weeks or days. All are normalised to years before age
strata are assigned.

### Handled but not silently

- `occurcountry` is `eu`, a region rather than a country, for 342 cases. Reported
  under its own label and not redistributed to member states, which the data does
  not support. Country totals are therefore incomplete and the report says so.
- 197 cases carry a source `duplicate` flag. Retained in all counts, because
  whether a flagged report is a true duplicate is a medical-review decision, not
  one a pipeline should make silently.
- 83 cases have no usable age and 28 no recorded sex. Reported as explicit
  `Unknown` strata rather than dropped from the denominator, because they are
  missing information about real cases, not absent cases.
- The supplied `patient_patientagegroup` is populated on 26 of 1,024 cases, so
  strata are derived from the numeric age field instead. The supplied field is
  unused, and the report says which was used.

---

## 10. Evaluating this at 1,000 reports

Eyeballing one report does not scale, and the failure that matters is not "reads
badly". It is "reads perfectly and is wrong". Five tiers, cheapest first, all but
the last automated:

**Tier 1, the analyses against golden values.** 100 tests, run per commit. The
numbers were derived by this pipeline and hand-checked, and where the vendor's
own reference output agrees that is recorded as corroboration in a comment. No
expected value was copied from it, and two deliberately disagree, with the reason
pinned in the test. This tier catches the class of bug that silently changes
every report at once.

**Tier 2, grounding score per report.** Already computed and already in the
document. At scale it becomes a gate. Any report below 100% does not ship, and
the violation names the sentence. This is a per-report signal that needs no human
and no reference output.

**Tier 3, structural completeness.** Every declared section present and
non-empty, every required fact computed, every unavailable fact stated in words
somewhere, no section exceeding its word budget, every table present. Cheap
assertions over the rendered document plus the fact store.

**Tier 4, differential and adversarial checks**, which is where I would spend
effort next:

- *Perturbation.* Re-run with a modified dataset, dropping 100 cases, and assert
  the affected figures moved and the unaffected ones did not. A report whose
  numbers do not follow its data is the failure no static check finds.
- *Cross-section consistency.* The same fact id appearing in two sections must be
  stated identically. Trivial to check because every claim is already tagged with
  the fact that licensed it.
- *Prompt-injection resistance.* Reaction terms are free text from an external
  source and flow into prompts. A term reading "ignore previous instructions" is
  the realistic attack, and the packet's structure is the defence. This needs a
  standing test with adversarial terms in a fixture dataset.

**Tier 5, human audit, sampled.** A stratified sample per model or prompt
version, read by a reviewer against the packet, scored for whether the section
answered its brief. The volume is set by the tiers above. They clear everything
mechanically checkable, so human attention goes to the only question left, which
is whether the model chose to say the *right* things rather than whether what it
said was true.

**What makes it attributable.** Appendix C records dataset hash, config file,
system-prompt hash, model id and review mode. An LLM judge would fit at Tier 5
for register and hedging, scored against the human sample rather than trusted on
its own, but it must never be the grounding check.

---

## 11. The real test: what survives PSUR, PBRER, DSUR, CSR

The differences between report types are which sections exist, what evidence each
needs, what each may claim, and how each should be worded. All four are data
here.

`reports/psur_mini.yaml` is a working second report type. It shares **no section**
with the PADER, uses a different regulatory frame, orders its content
differently, and required **zero** Python. Three tests assert this rather than
claiming it. They check that every fact it declares already existed, that the two
share no section ids, and that it generates and verifies end to end through the
same engine.

What survives unmodified: `loader.py`, `validate.py`, `facts.py`, `analyses.py`,
`spec.py`, `packet.py`, `llm.py`, `generate.py`, `verify.py`, `review.py` and
`render.py`, which is all of it. What changes is a YAML file.

What a real PSUR would additionally need is patient exposure, worldwide approval
status, signal evaluation and benefit-risk. Those are **new analyses**, one
decorated function each returning a `Fact`, which is the honest extension point,
and mostly they are new *data sources* rather than new code. None of them is a
new code path through the engine, and `psur_mini.yaml` claims none of them.

Section content is decoupled from analysis because sections name evidence by id
and analyses never learn who consumes them. That indirection is the single
structural decision the generalisation claim rests on.

---

## 12. Known limitations

**Scoped out deliberately, and declared in the report:**

- **No System Organ Class analysis.** No SOC field, no MedDRA hierarchy supplied.
  The reference report has one because that pipeline has a PT to SOC lookup we
  were not given. Adding it is a reference-data problem rather than an engine
  problem, needing a lookup table plus one analysis function.
- **No expectedness or labelledness.** Needs the approved label or CCDS.
- **No history of actions.** None supplied, so the section says so and invents
  nothing.
- **No cumulative or prior-period figures.** Reported as `not_available`, never
  as zero, because printing 0 asserts "we looked and found none" when the truth
  is "no earlier period was loaded". The reference report prints zeros in its
  cumulative columns. This one does not.

**Real gaps I would fix next, in order:**

1. **Drug-level analysis is absent.** The dataset carries suspect versus
   concomitant drug characterisation, indication and dosing, but as comma-packed
   parallel lists that do **not** align. One sampled row has 10 products against
   13 administration routes. Because they cannot be paired reliably, no analysis
   uses them, so the report cannot currently say anything about which cases named
   Bisoprolol as the suspect drug. That is a genuine content gap and the largest
   single one.
2. **The grain check reads one noun.** It looks at the first counting noun after
   a matched number. `"80 cases of a reaction"` is fine, but an inverted
   construction could slip past. It catches the realistic error. It is not a
   parser.
3. **Banned phrases are a list, not semantics.** It blocks the phrasings that
   actually occur, not every possible way of implying causality. A second-model
   claim-type classifier at Tier 5 would cover the rest, as an addition to the
   deterministic check and never a replacement.
4. **Percentages are only available where a denominator was declared.** A section
   wanting a share that nobody wired up gets a verifier rejection rather than the
   number. Correct, but it fails as a configuration error at run time rather than
   at load time.
5. **One dataset shape.** `loader.py` maps E2B/FAERS column names directly. A
   different source needs a field-mapping layer, which does not exist yet.
6. **No prompt-injection test.** Reaction terms are external free text reaching
   prompts. The structure defends against it, but nothing yet proves that.
7. **Single-language, single-locale.** Dates are ISO throughout and country
   values are used as supplied. The field mixes full names with ISO codes
   (`ie`, `hr`, `ro`), which is reported but not reconciled.

**Known about the model layer:** temperature is 0, but the same prompt may still
produce different text across model versions. The manifest records which model
produced a document, alongside the dataset and prompt hashes.

---

## 13. Repository map

```
src/genar/          the twelve modules in §3
reports/            pader.yaml · psur_mini.yaml     ← a report type is a file
prompts/system.md   the invariant rules
tests/              100 tests: golden numbers, fact invariants,
                    verifier negative tests, throttle/budget, reusability
                    fakes.py holds a template client so tests need no API key
version1/           DESIGN.md, how this evolves
output/             report_output.md, psur_mini_output.md
review/             the two gate files
architecture.md     diagram and the component split
.env.example        template for local settings
```

Not included: the dataset (redistribution notice), `.env`, `.venv/`, `.genar/`,
and `output/case_index.csv`. The last of those is a full restatement of the
supplied data, regenerated on every run.
