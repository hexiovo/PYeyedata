# Common eye-tracking metrics

## Background and context

The user requested all four missing metric groups identified in the source audit:
fixation-based AOI measures, reading measures, event distributions, and AOI hit
rates across observed trials/participants. Existing AOI entry/visit measures are
sample-based and must retain their IDs and semantics. The working tree already
contains unrelated workflow enhancements; preserve them. Work on master without
committing, packaging, changing dependencies, or changing archive/plan schemas.

## Acceptance criteria

- New metrics are selectable through the live catalogue and typed GUI parameters.
- Computed rows retain units, original validity, event source, scope, algorithm
  version, parameters, source identity, status and reason codes.
- AOI fixation assignment uses the time-weighted center of original valid samples
  within a reliable event, existing static geometry and explicit overlap policy.
  Events without sufficient continuous observed support remain unknown.
- Reading uses an explicitly confirmed ordered AOI annotation table (unique
  positive reading_order within stimulus/scope), with order increasing in reading
  progression for either ltr or rtl; no inferred word order or clinical claims.
- No-fixation/no-observation, censored first pass/go-past, source ambiguity,
  invalid samples, partition boundaries and insufficient SD samples stay explicit.
- Hit-rate denominators are observed exposure units only. Unknown units are
  reported; they are not converted into misses. Rates carry numerator,
  denominator, unknown counts, scope and supporting unit identities.
- Existing metric values, sample-entry semantics, archive and plan readability
  remain compatible. The algorithm version changes to invalidate old caches.
- Hand-calculated fixtures, deterministic generated/differential checks, GUI and
  pipeline/export/archive integration checks pass, followed by the repository's
  tools/manage.ps1 -Action Check scenarios.

## Implementation details

Add common_metrics.py for catalogue definitions, distribution calculations,
fixation AOI assignment, reading-sequence calculations and hit aggregation.
Integrate it into individual.py using existing scope/event/geometry helpers.
Use method_registry.py for the new parameter bindings; extend the existing
reading_annotations source mapping and enable its calculation use without
discarding legacy annotation tables. Keep invalid legacy reading annotations
archivable while blocking their use by the new reading measures.

Distribution summaries use equally weighted event observations, sample SD
(ddof=1), and linear quantiles at .10/.25/.75/.90. Durations use positive event
intersections with the current window; amplitudes use full event endpoints for
onsets in the half-open window. Blink intervals are onset-to-onset differences.

AOI TTFF uses fixation onsets within the window after an explicit reference;
AOI duration sums use clipped event intersections. Events beginning before the
reference/window do not become new first fixations. Per-exposure hit is binary
when known. Trial rates require explicit trial IDs; participant rates count each
participant once within session/stimulus/condition/segment strata, preserving
stream and dependence identifiers. Unknown support yields partial rates with
an explicit known-unit denominator, or unavailable when no unit is known.

Reading definitions follow SR Research's Reading Measures FAQ:
https://www.sr-research.com/support/thread-336.html
First pass requires progressive entry before any later AOI has been fixated.
Go-past sums fixation durations from initial entry through earlier/target regions
until the first progressive exit. Re-reading is total target fixation duration
minus first-pass target duration (explicit operational definition). A target
first encountered after a later region is skipped on first pass; no observed
progressive exit is right-censored rather than silently complete.

Add reusable fixed diagnostics in gui/diagnostic_common_metrics.py and call them
from reliability-smoke. Update version.md and the maintained handoff/metric
documentation. Evidence remains in the temporary directory printed by Check.

## Execution and validation

Implemented all four groups as 52 selectable metric IDs (34 distribution, 9 AOI,
9 reading), with no new dependencies or archive/plan schemas. Fixed the empty
overlap-query schema inconsistency exposed by a rejected-source regression.
Added conservative rejection of unbound/unplaced source events and filtering by
trial IDs supplied by segment labels. Legacy sample-entry metrics retain their
definitions. Old v1 plans receive only additive None/False defaults.

Final `tools/manage.ps1 -Action Check` passed all three scenarios on Windows x64
CPython 3.12.10. The new reliability suite recorded 1009 checks, including 840
distribution-oracle comparisons, 100 generated reading sequences, GUI/legacy
plan checks, pipeline dispatch, typed CSV and both archive modes. Seed: 20261027.
Evidence: C:/Users/DELL/AppData/Local/Temp/PYeyedata-check-94ac3ebe13614b6ca85e3faa020af5cd.
The deliberately censored fixture retains partial status; device/physiological
validation was not performed. No commit, release packaging or EXE was produced.
