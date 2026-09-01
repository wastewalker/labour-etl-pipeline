# How AI was used to build this

Built in a working session with [Claude Code](https://claude.com/claude-code) as
an active participant. This document records what that looked like, including
every place the model was wrong.

The [companion document in my API project](https://github.com/wastewalker/labour-indicators-api/blob/main/AI-WORKFLOW.md)
covers the general method. This one is about the lesson specific to ETL work,
which turned out to be sharper than I expected:

> **A model cannot write a parser for data it has not looked at, and neither can
> you. Everything else in this project followed from taking that seriously.**

---

## The rule that shaped the session

Before a single line of extractor code, I fetched all three sources and read
what actually came back. Not the documentation — the bytes.

That is a one-paragraph decision that saved the entire project, because the real
data disagreed with every reasonable assumption:

| What a reasonable person assumes | What the sources actually do |
| --- | --- |
| Missing values are blank, `null`, or `-` | The HTML table uses an **EN DASH** (U+2013) |
| Country names are country names | They carry a **narrow no-break space** (U+202F) before a footnote asterisk |
| A table has one header row | This one has **two**, with `rowspan`/`colspan`, and the year lives in the second |
| A column of unemployment rates is a column of unemployment rates | It is **four publishers side by side**, with different methodologies and different years |
| An API returns countries | It returns countries **and regional aggregates with country-shaped codes** |

Ask a model to "parse the Wikipedia unemployment table" without that groundwork
and you get a confident, plausible, wrong parser: single header row, ASCII hyphen
for missing values, `str.strip()` for the country name. It runs. It produces
numbers. The numbers are attributed to the wrong publisher and a fifth of them
silently vanish.

**The en dash is the clearest example.** I only found it by printing `ord()` on
the actual cell contents — the terminal rendered it as `?`, and the rendered page
in a browser shows something that looks exactly like a hyphen. No amount of
prompting finds that. Looking does.

---

## Decisions I made, not the model

**Rejected, skipped and unavailable are three different things.** This is the
spine of the design and the model's default collapses all three into one
`try/except` that counts failures. The distinction I insisted on:

- a bad row is counted and the load continues
- an out-of-scope row is counted *separately* and says nothing
- an unreadable source is abandoned and rolled back

The middle one is the non-obvious one, and it is the one that makes the other two
useful — see the eighty-rejection incident below.

**A partial run exits 0.** One flaky public website out of three is the condition
this pipeline exists to absorb. A scheduler that alerts every time a website is
briefly slow is a scheduler whose alerts get ignored.

**The ledger commits separately from the load.** If the two shared a transaction,
a failed load would roll back the record of its own failure. The model's first
draft had them in one transaction, and it looked completely reasonable.

**Country names are a lookup table, never a fuzzy match.** Offered `difflib` and
`rapidfuzz` as options. A near-miss here does not produce missing data, it
produces *wrong* data, attributed to a real country, that nobody will ever notice.

---

## What the model got wrong

### It could not have known about the User-Agent, and neither could I

The first fetch of Wikipedia returned **403**. Wikimedia blocks clients that do
not identify themselves, and the default `python-httpx` agent is exactly what it
throttles first. A User-Agent naming the project and linking to it works.

Then it got more interesting: on a re-test, the *original* agent worked too. The
403 was transient throttling from my own repeated requests during exploration,
not a permanent block. Two consequences, both in the shipped code: the User-Agent
carries contact information as Wikimedia asks, and 403 stays *out* of the
retryable status list, because retrying a genuine forbidden is pointless and the
real fix was the header.

I would have shipped a permanently broken source if I had accepted the first
result and moved on.

### It trusted the API to honour its own parameter

The World Bank source passes `date=2018:2100` and originally did no local year
filtering. A test I wrote with the expectation *first* — page two of a paginated
response containing 2017 rows — failed with `assert 14 == 12`.

The model's instinct was to fix the test. The test was right: `min_year` is a
property of this pipeline, not a request parameter, and an API that stopped
honouring the filter would quietly widen the load by thirty years. The local
filter shipped, described in the code as defence in depth rather than the primary
mechanism.

### Eighty rejections that were not rejections

This one only appeared by running the thing for real against the internet:

```
Source world_bank_api loaded 2930 rows (2930 changed, 80 rejected, 1230 out of scope)
  rejected: '' is not an ISO-3166 alpha-3 country code
```

Eighty rejections, every one of them noise: the API leaves `countryiso3code`
blank for some aggregate entities. Nothing was wrong.

That is a small bug with a large lesson. The rejection counter is only worth
reading if it is normally zero. Eighty rejections on a healthy run trains you to
ignore the field, and then the one genuine parse failure — the one you built the
counter *for* — sails past unseen. The fix was three characters (`not code or`).
Noticing it required a live run; the fixture-based unit tests were all green and
would have stayed green forever.

### Strict typing, and the temptation to switch it off

psycopg's `dict_row` types every column as `object`, which is honest — the driver
genuinely does not know. mypy in strict mode rejected `int(row["id"])` in five
places.

The model proposed widening the connection type to `dict[str, Any]`. That makes
the errors go away by making the type system stop asking. The fix that shipped is
two narrowing helpers, `as_int` and `to_float`, that check and raise. Loosening
strictness to make generated code compile is backwards; the flags are there
precisely to catch this class of thing while it is cheap.

### The linter caught an invisible bug I introduced

I had the model rewrite a regex through a patch script, and the nested escaping
mangled it into a non-raw string with broken escape sequences and *literal*
invisible Unicode spaces in the character class. It still worked, by luck.

Ruff's `RUF001` flagged it: "String contains ambiguous ` ` (NARROW NO-BREAK
SPACE)". The rewrite uses explicit ` ` escapes with a comment on each, which
is what it should have been from the start — a literal U+202F in source code is
indistinguishable from a space to everyone who reads it afterwards.

The wider lesson: **do not have a model rewrite source through generated patch
scripts**. Two layers of string escaping between intent and file is where silent
corruption lives. Precise edits, or write the file whole.

### It installed a dependency that earned nothing

`pydantic` went in during scaffolding out of habit. By the time the normalisation
layer existed, it validated nothing that `normalize.py` was not already
validating better, with reasons attached. Removed. Generated code accretes
plausible dependencies and nothing prompts you to notice.

### It wrote versions from memory

The versions that actually installed were ahead of what the model assumed —
**pandas 3.0**, psycopg 3.3, mypy 2.3, pytest 9. Same procedural fix as always:
install first, read the resolved versions back, write against those.

---

## AI in testing

**I chose the cases that encode a decision.** The ones that would have been wrong
by default:

- a source that fails halfway leaves **zero** rows, not the two that succeeded
- good source, broken source, good source — the *third* must still be attempted
- the ledger entry for a failure must survive the rollback of that failure
- a second run reports `changed=0` **and** leaves `updated_at` untouched
- an en dash is missing data; `'twelve'` is a broken parser; they are not the same

**The model expanded them well.** Given a named case it produced the boundary
table around it — the `parametrize` blocks over placeholders, out-of-range rates,
malformed years and aggregate codes are largely generated and they are good.

**What I did not accept:** mocking the database. Rollback, `ON CONFLICT`
semantics, `CHECK` violations and NUMERIC round-tripping are exactly what a mock
would have to fake, and a fake that got them all right would be a database.

**The trick that made the rollback tests possible.** To test that a mid-load
failure rolls back, you need a row the database refuses but the application would
never produce. The answer was to construct the frozen dataclass *directly*,
bypassing `Observation.create`, and let the `CHECK` constraint fire. That both
tests the rollback and proves the constraint is a real backstop rather than
decoration. The model's first suggestion was to mock the cursor to raise — which
would have tested the mock.

---

## AI in code review

Scoped, adversarial questions produced findings. "Review this file" produced
agreement.

- *"What does this count as a rejection that is not actually a problem?"* — this
  is what produced the `skipped` column, before the live run confirmed it.
- *"If the ledger write and the data write are in one transaction, what happens
  when the load fails?"* — the answer is a ledger that only contains successes.
- *"Which of these exceptions can a source raise that would take down the other
  two?"* — this produced the broad `except Exception` containment in the runner,
  which is deliberate and justified inline rather than lint-suppressed.

Ruff, mypy `--strict` and the coverage threshold did more reviewing than either
of us. Under those, a large share of what a human reviewer would otherwise chase
is a build failure instead — which is the actual argument for turning them all
the way up on an AI-assisted project.

---

## What I would tell a team doing ETL with AI

1. **Look at the data first.** Fetch it, print it, check the code points. Every
   real bug in this project lived in a character or a layout nobody would think
   to describe in a prompt.
2. **Run it against the real thing before you believe it.** The eighty-rejection
   bug was invisible to a green test suite and obvious within one live run.
3. **Decide your failure taxonomy yourself.** "Rejected", "skipped" and
   "unavailable" are a design decision. A model will collapse them and the
   result will look fine.
4. **Never loosen the type checker to make generated code compile.** The errors
   are the value.
5. **Edit source precisely.** Generated scripts that rewrite code through layers
   of escaping introduce bugs the tests will not catch and the linter might not.

The leverage is real. It is not in typing speed — it is in reaching a reviewable
draft of the mechanical 80% fast enough that the 20% that decides whether the
data is *correct* gets the attention it deserves.
