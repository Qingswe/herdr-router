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
- At that checkpoint, real browser visual verification was incomplete: IAB was unavailable and CUA did not allow Safari. The browser pass below closes this rendering follow-up.

## Browser verification — 2026-10-11

Completed with the standalone Google Chrome for Testing 143.0.7499.4 through native CUA controls and DevTools Responsive mode. Viewports were 1360×900 and 390×844 CSS pixels, at DPR 2, with the Chinese UI. The user's Chrome was not used. The pass includes the concurrently committed i18n implementation at `54feb71e09dfa0f2b330f433d43eef67848d7100` plus the CSS fixes below.

The server ran on loopback port 8797 with isolated config/state under `/private/tmp/router-ui-acceptance-20261011/`. Saves changed only this temporary policy. The test browser and server were closed after verification; no commit was created by this pass.

### Rendering and evidence

All five views were opened at both sizes and their rendered content inspected. Read-only Console measurements found `innerWidth === document.documentElement.scrollWidth === document.body.scrollWidth` in every view: 1360 on desktop and 390 on the narrow viewport. Controls, task entry, recommendation, profile editor, charts, settings and JSON remained within the page.

| View | Desktop evidence | 390px evidence |
| --- | --- | --- |
| 路由图 / profile editor | [Screenshot](ui-refresh-evidence/desktop-chart-editor.png) | [Screenshot](ui-refresh-evidence/mobile-chart-editor.png) |
| 维度 | [Screenshot](ui-refresh-evidence/desktop-dimensions.png) | [Screenshot](ui-refresh-evidence/mobile-dimensions.png) |
| 试运行 / recommendation | [Screenshot](ui-refresh-evidence/desktop-trial.png) | [Screenshot](ui-refresh-evidence/mobile-trial.png) |
| 设置 | [Screenshot](ui-refresh-evidence/desktop-settings.png) | [Screenshot](ui-refresh-evidence/mobile-settings.png) |
| JSON | [Screenshot](ui-refresh-evidence/desktop-json.png) | [Screenshot](ui-refresh-evidence/mobile-json.png) |
| Expanded diagnostics | [Full page](ui-refresh-evidence/desktop-diagnostics.png), [viewport](ui-refresh-evidence/desktop-diagnostics-viewport.png) | [Full page](ui-refresh-evidence/mobile-diagnostics.png), [table panned right](ui-refresh-evidence/mobile-diagnostics-scroll.png) |

Screenshot dimensions, SHA-256 hashes, source hashes, measurements and scope are recorded in [manifest.json](ui-refresh-evidence/manifest.json). PNGs are native DevTools captures. Full-page captures made after scrolling can place the sticky header at the captured scroll position; the desktop viewport capture shows its actual visible placement. Before/after images use different task/editor states, so they document specific layout defects rather than a pixel-identical comparison.

### Fixes found in the browser

- The draft toolbar selector required a `.change-summary` class that the toolbar did not have. Targeting `#draft-controls` applies the intended surface, width and spacing on both layouts. [Before](ui-refresh-evidence/desktop-chart-before.png), [after](ui-refresh-evidence/desktop-chart-editor.png).
- At 390px, the recommendation's score column wrapped the final digit of values such as `0.1741`. An intrinsic-width column and `white-space: nowrap` keep the values on one line. [Before](ui-refresh-evidence/mobile-trial-before.png), [after](ui-refresh-evidence/mobile-trial.png).
- The expanded ranking table squeezed rank indices and scores into multiple lines. A 520px minimum table width and nonwrapping numeric cells preserve readability inside the existing scroll container. [Before](ui-refresh-evidence/mobile-diagnostics-before.png), [after](ui-refresh-evidence/mobile-diagnostics.png). At 390px the container measured 290px, the table 520px, and an actual horizontal drag reached `scrollLeft = 230` while the page stayed at `scrollX = 0` and width 390. Desktop's 514px container also keeps its small overflow local.

### Workflows exercised

- Expanded task examples, selected the crash example, entered a multiline task, and ran it with ⌘+Enter. Jev was disabled for keyword evaluation. The final server-scored result recommended Sol medium with total penalty `0.2091` (difficulty `0.035`, cost `0.126`, time `0.0481`); the next score was `0.2508`, a gap of `0.0417`. Recommendation and diagnostic numbers agreed. No agent command was executed.
- Expanded startup command and diagnostics with visible keyboard focus; the copy control reported “启动命令已复制”. Editing task/policy marked the existing trial result stale.
- Searched for “Sol” (7/34 profiles), applied the enabled filter (4/34), selected Sol medium, and edited its numeric coordinate. Incrementing the difficulty slider changed `2.57` to `3.57` and updated the numeric field. Undo and redo **buttons** restored `2.57` and `3.57`; saving acknowledged the new temporary baseline.
- Set `scoring.under_penalty` to `-1` and attempted a real save. The server returned HTTP 422 with `scoring.under_penalty must be a number >=0`; the draft remained dirty and the error stayed inside the 390px page. [Error screenshot](ui-refresh-evidence/mobile-save-error.png). “放弃修改” plus its confirmation restored the saved value `4` and a clean draft. The final browser state reported “草稿与已保存版本一致”.

### Regression checks and scope

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`: **197 passed**.
- `node tests/test_frontend.js`: **32 passed**, including the interaction and i18n coverage.
- `node --check web/app.js`, `node --check web/i18n.js`, and `git diff --check`: passed.

This closes the desktop/390px Chromium rendering follow-up. The narrow layout was viewport emulation, not a physical phone. WebKit/Firefox, a live Jev request, populated routing history, profile dragging and harness switching were not manually exercised in this browser pass; the existing VM suite remains the automated evidence for the relevant state transitions. Native keyboard undo/redo shortcuts were not counted as verified.
