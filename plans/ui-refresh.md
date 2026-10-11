# Router UI refresh

## Goal

Keep the dark, monochrome shell and vendor-coloured routing data. Make the existing editor easier to scan, try tasks, inspect results, find profiles, and edit safely. Keep native JS/SVG, existing API semantics, routing policy, and web editor's inability to dispatch.

## Parallel ownership

- Visual worker: `web/style.css`, `web/index.html` only.
- Interaction worker: `web/app.js` only. Do not reformat the entire file.
- Validation worker: `tests/test_frontend.js` only; read-only review of other changes.
- Coordinator: integration, regression checks, README updates.
- Do not commit, change user configuration, restart the user's web server, or touch other workers' files.

## Shared UI contract

Preserve existing IDs and classes. New optional hooks: `.workspace-intro`, `.task-composer`, `.task-examples`, `.result-summary`, `.result-meta`, `.result-reason`, `.result-details`, `.penalty-stack`, `.penalty-segment`, `.profile-tools`, `.profile-search`, `.profile-filter`, `.profile-empty`, `.editor-section`, `.editor-section-body`, `.chart-layers`, `.chart-tooltip`, `.change-summary`, `.stale-result`, `.mobile-view-switch`. Use semantic HTML, explicit labels, native `details/summary` when suitable. New controls should remain usable even if only existing CSS is present.

## Acceptance

1. Clear type/spacing/surface hierarchy; comfortable controls and visible keyboard focus; desktop and 390px layout should fit without horizontal page overflow.
2. Multiline task entry with examples; a prominent recommendation with model/agent/effort, honest reason based on actual scores, explicit lower-is-better penalties, expandable diagnostics and a copy-command control. Retain draft trial semantics and mark results stale after policy changes.
3. Profile search/filter and grouped list; distinguish disabled, missing CLI, and zero coverage on current map slice. Editor grouped into clear basic/coordinate/type/advanced sections; numeric coordinate editing alongside sliders.
4. Chart controls for history, disabled profiles and labels; selected model emphasis. Retain dragging, harness switching, actual server-scored territory, axes/choice filters and special profiles. Explain the current map is a slice.
5. Rename discard-all to 放弃修改. Add actual undo/redo if reliably achievable in the existing change path, with proper drag/input grouping and save-baseline semantics. Show a small meaningful draft change summary.

## Validation

Run Python unittest suite and JS syntax validation. Exercise key workflows where a browser is available: trial, selection, filter/search, coordinate edits, undo/redo, save errors, tabs, narrow layout. Use isolated temporary config/state and a separate local test port if starting a server. Browser/computer interaction must use available CUA APIs; do not control the user's Chrome or bypass this with external automation. Report actual checks and limitations.

## Implemented behavior

- Recommendation appears directly after task entry; diagnostics and startup command are expandable.
- Draft history retains 100 operations. Discard returns to the acknowledged saved baseline and is itself reversible. Deferred text fields retain native text undo until committed.
- Save/trial use request snapshots; later edits stay dirty or mark results stale. Failed map requests can retry the same policy.
- Map layers are display controls, separate from routing enablement. Coverage filters require a current server map.
- CSS follows the generated DOM: a full-width draft toolbar, readable penalty chips, and an in-flow map readout with an empty state.

## Verification recorded by coordinator

- Existing Python suite: 58 tests passed (isolated web API tests needed local-port permission).
- Frontend VM behavior suite: 26 tests passed, including undo grouping, async saves, failures/retries, stale results, filters and map races.
- `node --check web/app.js` and `git diff --check`: passed.
- Real browser visual verification remains incomplete: IAB was unavailable and CUA did not allow Safari. Desktop and 390px styles were reviewed in source; actual rendering still needs verification.
