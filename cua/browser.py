"""Browser session, page observation, and locator resolution.

Observation strategy: we build a compact, structured text snapshot from the
page's *semantics* (roles, accessible names, labels, visible text) rather
than relying on raw screenshots. Rationale: this is the representation that
still exists when the DOM is hostile (framesets, tables, no test IDs) and it
is the same shape an OS accessibility API would give us for a desktop app —
which keeps the observe/act seam surface-agnostic. Screenshots are captured
as *evidence*, not as the primary perception channel.

The snapshot assigns each interactive element an ephemeral ref (``e1``,
``e2``…). The LLM acts on refs; the executor maps refs back to live elements
via recorded metadata. The same metadata is what the recorder later turns
into durable locator chains for the artifact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from playwright.sync_api import Browser, Locator, Page, sync_playwright

from cua.schema import ElementTarget, Locator as SchemaLocator, LocatorStrategy

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class BrowserSession:
    """Owns the Playwright lifecycle. One session == one live browser page.

    The same live page object is shared by the discovery agent, the replay
    engine, and the human-handoff console — control transfer is a matter of
    *who is issuing commands*, never of opening a new session.
    """

    def __init__(self, headed: bool = False):
        self._pw = sync_playwright().start()
        launch_kwargs: dict = {"headless": not headed}
        exe = os.environ.get("CUA_CHROMIUM_PATH")
        if exe:
            launch_kwargs["executable_path"] = exe
        self.browser: Browser = self._pw.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context()
        self.page: Page = self.context.new_page()
        # Some targets (MERIDIAN CORE) signal exceptional states via the
        # HTTP status of the main-frame response rather than distinguishing
        # them purely in visible text. Track the last such status directly
        # on the page object so `probe_holds` can check it — no signature
        # change needed at any of its call sites.
        self.page._cua_last_status = None  # type: ignore[attr-defined]

        def _track_status(response) -> None:
            try:
                if response.request.is_navigation_request() and (
                    response.frame == self.page.main_frame
                ):
                    self.page._cua_last_status = response.status  # type: ignore[attr-defined]
            except Exception:
                pass

        self.page.on("response", _track_status)

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Observation (the "observe" of observe -> decide -> act)
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = r"""
() => {
  const results = [];
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== 'HTML') {
      let seg = el.tagName.toLowerCase();
      if (el.id) { seg += '#' + el.id; parts.unshift(seg); break; }
      const siblings = Array.from(el.parentNode ? el.parentNode.children : [])
        .filter(s => s.tagName === el.tagName);
      if (siblings.length > 1) seg += `:nth-of-type(${siblings.indexOf(el) + 1})`;
      parts.unshift(seg);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  // Returns [name, isRealAccName]. "Real" = contributes to the browser's
  // ARIA accessible-name computation (usable by role-based locators at
  // replay time). Heuristic names (td-proximity, name attr) are for human/
  // LLM readability only — replay must target those elements another way.
  const accName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return [aria, true];
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && (el.type === 'submit' || el.type === 'button'))
      return [el.value || '', true];
    if (tag === 'button' || tag === 'a') return [(el.innerText || '').trim(), true];
    if (el.id) {
      const lab = document.querySelector(`label[for="${el.id}"]`);
      if (lab) return [(lab.innerText || '').trim(), true];
    }
    const ph = el.getAttribute('placeholder');
    if (ph) return [ph, true];
    // Legacy layout: label text usually lives in the previous table cell.
    const cell = el.closest('td');
    if (cell && cell.previousElementSibling)
      return [(cell.previousElementSibling.innerText || '').trim().replace(/:$/, ''), false];
    return [el.getAttribute('name') || '', false];
  };
  const role = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.href) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (el.type === 'submit' || el.type === 'button') return 'button';
      if (el.type === 'checkbox') return 'checkbox';
      if (el.type === 'radio') return 'radio';
      if (el.type === 'hidden') return null;
      return 'textbox';
    }
    return null;
  };
  const selector = 'a[href], button, input, select, textarea, [onclick], [role]';
  let i = 0;
  for (const el of document.querySelectorAll(selector)) {
    const r = role(el);
    if (!r || !isVisible(el)) continue;
    i += 1;
    const [nm, realName] = accName(el);
    const entry = {
      ref: 'e' + i,
      role: r,
      name: nm.slice(0, 120),
      name_is_real: realName,
      tag: el.tagName.toLowerCase(),
      name_attr: el.getAttribute('name') || null,
      id_attr: el.id || null,
      css_path: cssPath(el),
      text: (el.innerText || el.value || '').trim().slice(0, 120),
    };
    if (el.tagName.toLowerCase() === 'select') {
      entry.options = Array.from(el.options).map(o => o.value).filter(v => v);
    }
    // Row context: when this control sits inside a table row (e.g. a "View"
    // or "Edit" link repeated once per row), its role+name/text locator is
    // identical across every row and therefore ambiguous. Capture the
    // row's cell texts here too -- same shape as the readable-cell scan
    // below -- so the recorder can anchor on a declared input's value
    // found in this row, the same way it already does for data cells.
    const tr = el.closest('tr');
    if (tr) {
      entry.row_texts = Array.from(tr.children)
        .filter(c => c.tagName === 'TD')
        .map(c => (c.innerText || '').trim());
    }
    results.push(entry);
  }
  // Readable data cells: legacy apps put values in label/value table rows.
  // A cell qualifies if it is a pure-text leaf whose left sibling looks like
  // a label — that label becomes the cell's name, so the agent can `read` it
  // and the recorder can build a label-relative locator for it. A cell in
  // the table's FIRST column has no left sibling to use as a label at all
  // (previousElementSibling is null) -- fall back to that column's header
  // cell instead, so the first column of a data table is readable too, not
  // silently invisible to the agent. The two cases build different locator
  // kinds downstream (see label_source): a sibling label is itself a cell
  // in the same row (a real label:value pair), so the row can be found by
  // that label's text; a header label lives in a different row entirely,
  // so it can only ever identify a *column*, not anchor a row lookup.
  let cells = 0;
  for (const td of document.querySelectorAll('td')) {
    if (cells >= 40) break;
    if (!isVisible(td)) continue;
    if (td.querySelector('a, input, select, button, textarea, table')) continue;
    const text = (td.innerText || '').trim();
    if (!text || text.length > 100) continue;
    const row = td.parentElement;
    const rowCells = Array.from(row.children).filter(c => c.tagName === 'TD');
    const colIndex = rowCells.indexOf(td) + 1;
    const prev = td.previousElementSibling;
    let label = null;
    let labelSource = null;
    if (prev && prev.tagName === 'TD') {
      label = (prev.innerText || '').trim().replace(/:$/, '');
      labelSource = 'sibling';
    } else {
      const table = td.closest('table');
      const headerRow = table ? table.querySelector('tr') : null;
      if (headerRow && headerRow !== row) {
        const headerCells = Array.from(headerRow.children)
          .filter(c => c.tagName === 'TH' || c.tagName === 'TD');
        const headerCell = headerCells[colIndex - 1];
        if (headerCell) {
          label = (headerCell.innerText || '').trim().replace(/:$/, '');
          labelSource = 'header';
        }
      }
    }
    if (!label || label.length > 60) continue;
    i += 1; cells += 1;
    // Full row, in column order -- lets the recorder recognize when a
    // *different* cell in this same row (not just the immediately
    // preceding one) is the row's real stable identity, e.g. a Share ID
    // column in a multi-column data table where every column is data and
    // no single column is a durable "label" the way a label:value form is.
    const rowTexts = rowCells.map(c => (c.innerText || '').trim());
    results.push({
      ref: 'e' + i,
      role: 'cell',
      name: label,
      tag: 'td',
      name_attr: null,
      id_attr: null,
      css_path: cssPath(td),
      text: text.slice(0, 120),
      prev_label: label,
      col_index: colIndex,
      row_texts: rowTexts,
      label_source: labelSource,
    });
  }
  return {
    title: document.title,
    elements: results,
    visible_text: (document.body ? document.body.innerText : '').slice(0, 4000),
  };
}
"""


@dataclass
class ElementInfo:
    ref: str
    role: str
    name: str
    tag: str
    name_attr: str | None
    id_attr: str | None
    css_path: str
    text: str
    options: list[str] = field(default_factory=list)
    prev_label: str | None = None
    col_index: int | None = None
    name_is_real: bool = True
    row_texts: list[str] = field(default_factory=list)
    label_source: str | None = None  # "sibling" | "header" | None


@dataclass
class Snapshot:
    url: str
    title: str
    elements: list[ElementInfo]
    visible_text: str

    def element(self, ref: str) -> ElementInfo | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def to_prompt_text(self) -> str:
        """Compact textual observation for the LLM."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "", "INTERACTIVE ELEMENTS:"]
        for e in self.elements:
            desc = f"  [{e.ref}] {e.role} \"{e.name}\""
            if e.options:
                desc += f" options={e.options}"
            if e.text and e.text != e.name:
                desc += f" text=\"{e.text[:60]}\""
            lines.append(desc)
        lines += ["", "VISIBLE TEXT:", self.visible_text]
        return "\n".join(lines)


def take_snapshot(page: Page) -> Snapshot:
    raw = page.evaluate(_SNAPSHOT_JS)
    elements = [
        ElementInfo(
            ref=e["ref"],
            role=e["role"],
            name=e.get("name") or "",
            tag=e["tag"],
            name_attr=e.get("name_attr"),
            id_attr=e.get("id_attr"),
            css_path=e.get("css_path") or "",
            text=e.get("text") or "",
            options=e.get("options") or [],
            prev_label=e.get("prev_label"),
            col_index=e.get("col_index"),
            name_is_real=e.get("name_is_real", True),
            row_texts=e.get("row_texts") or [],
            label_source=e.get("label_source"),
        )
        for e in raw["elements"]
    ]
    return Snapshot(
        url=page.url,
        title=raw["title"],
        elements=elements,
        visible_text=raw["visible_text"],
    )


# ---------------------------------------------------------------------------
# Locator chain construction (discovery -> artifact)
# ---------------------------------------------------------------------------


def build_locator_chain(info: ElementInfo) -> ElementTarget:
    """Turn observed element metadata into a durable fallback chain.

    Order encodes the robustness argument documented in cua/schema.py:
    semantics first (role+name), then attribute CSS, then visible text,
    then structural path as last resort / drift canary.

    Data cells are special-cased: their accessible name would be their
    *value* (which varies run to run), so the durable identity is the
    label/value relationship — "the cell to the right of the cell that says
    'Confirmation Ref'" — expressed as a label-relative selector, with the
    structural path as fallback.
    """
    locators: list[SchemaLocator] = []
    if info.role == "cell" and info.prev_label and info.col_index:
        if info.label_source == "header":
            # The label came from the table's header row, not a same-row
            # sibling -- it identifies a *column*, not a row, so a
            # `tr:has(td:text-is(label))` selector would never match (that
            # text lives in the header row, not this data row). There is no
            # semantic row-identity to anchor on here; the structural path
            # (this row, this column) is the only correct locator -- which
            # is also the right read for "the top/most recent row", a
            # genuinely positional concept, not a fallback for a missing one.
            if info.css_path:
                locators.append(
                    SchemaLocator(
                        strategy=LocatorStrategy.DOM_PATH,
                        value=info.css_path,
                        note=(
                            f"Primary: structural position of the '{info.prev_label}' "
                            "column in this row. No label:value pair exists in the "
                            "row itself (the label is the column header), so "
                            "position is the only valid locator here."
                        ),
                    )
                )
            return ElementTarget(
                description=f'the "{info.prev_label}" cell in this row',
                locators=locators or [SchemaLocator(strategy=LocatorStrategy.CSS, value=info.css_path or "", note="Fallback")],
            )
        locators.append(
            SchemaLocator(
                strategy=LocatorStrategy.CSS,
                value=(
                    f'tr:has(td:text-is("{info.prev_label}")) '
                    f"> td:nth-of-type({info.col_index})"
                ),
                note=(
                    "Primary for data cells: label-relative position. The "
                    "label text is UI copy (stable); the value varies per "
                    "run, so it must not be part of the locator."
                ),
            )
        )
        if info.css_path:
            locators.append(
                SchemaLocator(
                    strategy=LocatorStrategy.DOM_PATH,
                    value=info.css_path,
                    note="Last resort: structural path (drift canary).",
                )
            )
        return ElementTarget(
            description=f'the value cell next to "{info.prev_label}"',
            locators=locators,
        )
    if info.name and info.name_is_real:
        # Only when the name is a *genuine* accessible name — a heuristic
        # (td-proximity) name is invisible to the browser's role engine and
        # would make the primary locator fail on every replay.
        locators.append(
            SchemaLocator(
                strategy=LocatorStrategy.ROLE,
                value=info.role,
                name=info.name,
                note="Primary: role + accessible name; closest to human perception, survives markup refactors.",
            )
        )
    if info.name_attr:
        locators.append(
            SchemaLocator(
                strategy=LocatorStrategy.CSS,
                value=f'{info.tag}[name="{info.name_attr}"]',
                note="Fallback: form-field name attribute; stable in server-rendered apps (server depends on it).",
            )
        )
    if info.id_attr:
        locators.append(
            SchemaLocator(
                strategy=LocatorStrategy.CSS,
                value=f"#{info.id_attr}",
                note="Fallback: element id.",
            )
        )
    if info.role == "link" and info.text:
        locators.append(
            SchemaLocator(
                strategy=LocatorStrategy.TEXT,
                value=info.text,
                note="Fallback: visible link text; stable until copy changes.",
            )
        )
    if info.css_path:
        locators.append(
            SchemaLocator(
                strategy=LocatorStrategy.DOM_PATH,
                value=info.css_path,
                note="Last resort: structural path. Brittle by design — resolving here is logged as UI-drift warning.",
            )
        )
    label = info.name or info.text or info.name_attr or info.tag
    return ElementTarget(
        description=f"the {info.role} \"{label}\" ({info.tag})",
        locators=locators,
    )


# ---------------------------------------------------------------------------
# Locator resolution (artifact -> live element, replay side)
# ---------------------------------------------------------------------------


class ResolutionError(Exception):
    def __init__(self, target: ElementTarget, attempts: list[str]):
        self.target = target
        self.attempts = attempts
        super().__init__(
            f"no locator resolved for {target.description!r}; attempts: {attempts}"
        )


def resolve_target(
    page: Page, target: ElementTarget, timeout_ms: int = 3000
) -> tuple[Locator, int, str]:
    """Resolve an ElementTarget to a live, unique, visible element.

    Tries each locator in declared order. A locator is accepted only if it
    matches exactly one visible element — ambiguity means we skip to the
    next strategy rather than guess (determinism over cleverness). Returns
    ``(locator, rank, describe)`` where rank>0 signals degraded resolution.
    """
    attempts: list[str] = []
    per_try = max(timeout_ms // max(len(target.locators), 1), 500)
    for rank, loc in enumerate(target.locators):
        describe = f"{loc.strategy.value}:{loc.value}" + (
            f" name={loc.name!r}" if loc.name else ""
        )
        try:
            if loc.strategy == LocatorStrategy.ROLE:
                handle = page.get_by_role(loc.value, name=loc.name, exact=True)
            elif loc.strategy == LocatorStrategy.LABEL:
                handle = page.get_by_label(loc.value)
            elif loc.strategy == LocatorStrategy.TEXT:
                handle = page.get_by_text(loc.value, exact=True)
            else:  # CSS and DOM_PATH are both CSS-selector engines here
                handle = page.locator(loc.value)
            count = handle.count()
            if count == 1 and handle.first.is_visible():
                return handle.first, rank, describe
            if count == 0 and loc.strategy == LocatorStrategy.ROLE:
                # One retry without exact matching (whitespace tolerance).
                handle = page.get_by_role(loc.value, name=loc.name, exact=False)
                if handle.count() == 1 and handle.first.is_visible():
                    return handle.first, rank, describe + " (inexact)"
            attempts.append(f"{describe} -> {count} match(es)")
        except Exception as exc:  # locator engine errors -> try next strategy
            attempts.append(f"{describe} -> error: {exc.__class__.__name__}")
    # Give the page one settle window, then a final pass (transient loads).
    page.wait_for_timeout(min(per_try, 1000))
    for rank, loc in enumerate(target.locators):
        try:
            if loc.strategy == LocatorStrategy.ROLE:
                handle = page.get_by_role(loc.value, name=loc.name, exact=False)
            elif loc.strategy == LocatorStrategy.LABEL:
                handle = page.get_by_label(loc.value)
            elif loc.strategy == LocatorStrategy.TEXT:
                handle = page.get_by_text(loc.value, exact=False)
            else:
                handle = page.locator(loc.value)
            if handle.count() == 1 and handle.first.is_visible():
                return handle.first, rank, f"{loc.strategy.value}:{loc.value} (retry)"
        except Exception:
            continue
    raise ResolutionError(target, attempts)


# ---------------------------------------------------------------------------
# State probes (checkpoint / detector evaluation)
# ---------------------------------------------------------------------------


def probe_holds(page: Page, probe, timeout_ms: int = 0) -> bool:
    """Evaluate a StateProbe against current page state.

    With a timeout budget, polls until the probe holds or the budget is
    exhausted — this is the only waiting primitive replay uses (condition-
    based, never blind sleeps).
    """
    import re as _re
    import time as _time

    deadline = _time.monotonic() + timeout_ms / 1000.0
    while True:
        ok = True
        if probe.url_pattern and not _re.search(probe.url_pattern, page.url):
            ok = False
        if ok and getattr(probe, "http_status", None) is not None:
            if getattr(page, "_cua_last_status", None) != probe.http_status:
                ok = False
        if ok and probe.text:
            try:
                body = page.inner_text("body", timeout=1000)
            except Exception:
                body = ""
            if probe.text not in body:
                ok = False
        if ok and probe.target is not None:
            try:
                resolve_target(page, probe.target, timeout_ms=800)
            except ResolutionError:
                ok = False
        if ok:
            return True
        if _time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)
