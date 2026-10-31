/**
 * Week in Review — shared formatters and renderer (US-490.3, US-490.4)
 * Spec: docs/specs/weekly-week-in-review.md §1, §2, §3–§8
 *
 * Used by the homepage briefing (index.html) and the five section-page
 * reviews (loadSectionReview below).
 *
 * Week label and footer dates come from the server's ET week fields
 * (week_start, week_end, through, published_at, next_review_at). They are
 * formatted from their YYYY-MM-DD parts, never through the browser clock:
 * new Date('2026-10-30') is Oct 29 in any US timezone.
 *
 * Model output is only ever written with textContent, so a structured field
 * can never be parsed as HTML.
 */
const BRIEFING_STALE_MS = 8 * 24 * 60 * 60 * 1000;
const WEEK_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEK_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const WATCH_DAY_NAMES = { Mon: 'Monday', Tue: 'Tuesday', Wed: 'Wednesday', Thu: 'Thursday', Fri: 'Friday' };

// "2026-10-30" or "2026-10-30T17:45:00-04:00" → {y, m, d, dow}; the date part
// of an ET timestamp is already the ET calendar day.
function parseWeekDate(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || '');
    if (!m) return null;
    const y = +m[1], mo = +m[2] - 1, d = +m[3];
    return { y: y, m: mo, d: d, dow: new Date(Date.UTC(y, mo, d)).getUTCDay(), iso: m[0] };
}

function formatShortDay(p) {
    return WEEK_DAYS[p.dow] + ', ' + WEEK_MONTHS[p.m] + ' ' + p.d;
}

// "Week of Oct 26–30, 2026" / "Week of Oct 29 – Nov 2, 2026" /
// "Week of Dec 29, 2025 – Jan 2, 2026" / "Week to date · through Wed, Nov 4"
function formatWeekLabel(data) {
    const start = parseWeekDate(data.week_start);
    const end = parseWeekDate(data.week_end);
    if (!start || !end) return null;
    if (data.week_to_date) {
        const through = parseWeekDate(data.through) || end;
        return 'Week to date · through ' + formatShortDay(through);
    }
    if (start.y !== end.y) {
        return 'Week of ' + WEEK_MONTHS[start.m] + ' ' + start.d + ', ' + start.y +
            ' – ' + WEEK_MONTHS[end.m] + ' ' + end.d + ', ' + end.y;
    }
    if (start.m !== end.m) {
        return 'Week of ' + WEEK_MONTHS[start.m] + ' ' + start.d +
            ' – ' + WEEK_MONTHS[end.m] + ' ' + end.d + ', ' + end.y;
    }
    return 'Week of ' + WEEK_MONTHS[start.m] + ' ' + start.d + '–' + end.d + ', ' + end.y;
}

// Section collapsed header: "Oct 26–30" / "Oct 29 – Nov 2". A week-to-date
// review ends at `through`, so the range never claims days not yet covered.
function formatCompactRange(data) {
    const start = parseWeekDate(data.week_start);
    const end = parseWeekDate(data.week_to_date ? (data.through || data.week_end) : data.week_end);
    if (!start || !end) return null;
    if (start.iso === end.iso) return WEEK_MONTHS[start.m] + ' ' + start.d;
    if (start.y === end.y && start.m === end.m) {
        return WEEK_MONTHS[start.m] + ' ' + start.d + '–' + end.d;
    }
    return WEEK_MONTHS[start.m] + ' ' + start.d + ' – ' + WEEK_MONTHS[end.m] + ' ' + end.d;
}

// "Published Fri, Oct 30 · Next review Fri, Nov 6" (or "Updated …" after a manual regenerate)
function formatFreshness(data) {
    const published = parseWeekDate(data.published_at);
    const next = parseWeekDate(data.next_review_at);
    const parts = [];
    if (published) {
        parts.push((data.run_type === 'scheduled' ? 'Published ' : 'Updated ') + formatShortDay(published));
    }
    if (next) parts.push('Next review ' + formatShortDay(next));
    return parts.join(' · ');
}

// Stale only when a whole Friday run was missed. Absolute timestamps, so the
// reader's timezone does not matter; "now" is the browser clock.
function isBriefingStale(data) {
    const published = Date.parse(data.published_at || '');
    return !isNaN(published) && (Date.now() - published) > BRIEFING_STALE_MS;
}

function makeEl(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
}

// Part label: <h4> under the homepage's <h3>, <h3> under a section page's <h2>.
function makePart(label, tag) {
    const part = makeEl('div', 'briefing-part');
    part.appendChild(makeEl(tag, 'briefing-part__label', label));
    return part;
}

function makeWatchItem(item) {
    const li = makeEl('li', 'briefing-watch__item');
    const chip = makeEl('span', 'briefing-watch__day');
    if (item.day === 'Ongoing') {
        chip.setAttribute('aria-label', 'Ongoing');
        chip.appendChild(makeEl('span', null, '—')).setAttribute('aria-hidden', 'true');
        chip.appendChild(makeEl('span', 'visually-hidden', 'Ongoing: '));
    } else {
        chip.appendChild(makeEl('span', null, item.day)).setAttribute('aria-hidden', 'true');
        chip.appendChild(makeEl('span', 'visually-hidden', (WATCH_DAY_NAMES[item.day] || item.day) + ': '));
    }
    li.appendChild(chip);
    const text = makeEl('span', 'briefing-watch__text');
    text.appendChild(makeEl('span', 'briefing-watch__event', item.event || ''));
    if (item.reason) text.appendChild(makeEl('span', 'briefing-watch__reason', item.reason));
    li.appendChild(text);
    return li;
}

// Section-page inline form: <strong>WED</strong> FOMC decision — reason.
// The abbreviation is shown, the full day name is what a screen reader hears.
function makeInlineWatchItem(item) {
    const li = makeEl('li', 'briefing-watch__item');
    const day = makeEl('strong', 'briefing-watch__day-inline');
    if (item.day === 'Ongoing') {
        day.textContent = 'Ongoing';
    } else {
        day.appendChild(makeEl('span', null, item.day)).setAttribute('aria-hidden', 'true');
        day.appendChild(makeEl('span', 'visually-hidden', WATCH_DAY_NAMES[item.day] || item.day));
    }
    li.appendChild(day);
    li.appendChild(document.createTextNode(' '));
    li.appendChild(makeEl('span', 'briefing-watch__event', item.event || ''));
    if (item.reason) li.appendChild(document.createTextNode(' — ' + item.reason));
    return li;
}

// Builds the headline + three parts with textContent only, so model output is
// never parsed as HTML. Renders whichever parts exist; returns null if none do.
// Homepage (default): <h4> labels, chip watch list in a side rail.
// Section pages ({ partTag: 'h3', inlineWatch: true }): <h3> labels, inline
// watch items stacked under the other parts, no rail.
function buildStructuredReview(s, opts) {
    const partTag = (opts && opts.partTag) || 'h4';
    const inlineWatch = Boolean(opts && opts.inlineWatch);
    const fragment = document.createDocumentFragment();
    if (s.headline) fragment.appendChild(makeEl('p', 'briefing-headline', s.headline));

    const main = makeEl('div', 'briefing-review__main');
    const changed = Array.isArray(s.changed) ? s.changed.filter(Boolean) : [];
    if (changed.length) {
        const part = makePart('What changed', partTag);
        const ul = makeEl('ul', 'briefing-changed');
        changed.forEach(function (text) { ul.appendChild(makeEl('li', null, text)); });
        part.appendChild(ul);
        main.appendChild(part);
    }
    const why = typeof s.why === 'string' ? s.why.split(/\n\s*\n/).filter(function (p) { return p.trim(); }) : [];
    if (why.length) {
        const part = makePart('Why it matters', partTag);
        why.forEach(function (text) { part.appendChild(makeEl('p', null, text.trim())); });
        main.appendChild(part);
    }
    const watch = Array.isArray(s.watch) ? s.watch.filter(function (w) { return w && w.event; }) : [];
    let rail = null;
    if (watch.length) {
        const part = makePart('What to watch next week', partTag);
        const ol = makeEl('ol', inlineWatch ? 'briefing-watch briefing-watch--inline' : 'briefing-watch');
        watch.forEach(function (item) { ol.appendChild(inlineWatch ? makeInlineWatchItem(item) : makeWatchItem(item)); });
        part.appendChild(ol);
        if (inlineWatch) {
            main.appendChild(part);
        } else {
            rail = part;
            rail.classList.add('briefing-review__rail');
        }
    }

    const hasMain = main.childNodes.length > 0;
    if (!hasMain && !rail) return null;
    // Two columns only when both sides have content, so a review without a
    // watch list never leaves an empty 38% rail.
    const review = makeEl('div', 'briefing-review' + (hasMain && rail ? ' briefing-review--with-rail' : ''));
    if (hasMain) review.appendChild(main);
    if (rail) review.appendChild(rail);
    fragment.appendChild(review);
    return fragment;
}

// Sets a <time> week label from the server fields, or hides it when a record
// has none (legacy JSON), so it never shows "undefined" or "Invalid Date".
function applyWeekLabel(el, text, data) {
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
    if (text) {
        el.setAttribute('datetime', data.week_start + '/' + data.week_end);
    } else {
        el.removeAttribute('datetime');
    }
}

/* ─── Section pages (US-490.4) ─────────────────────────────────────────── */

const SECTION_REVIEW_OPTIONS = { partTag: 'h3', inlineWatch: true };

// Markdown-only records render as the section pages always have.
function renderSectionMarkdown(contentEl, summary) {
    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true, sanitize: false });
        contentEl.innerHTML = marked.parse(summary);
    } else {
        contentEl.innerHTML = summary
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            .replace(/\n/g, '<br>');
    }
}

// Loads a section review into the markup the section templates share:
//   #<prefix>-review-range    compact range in the collapsed header (≥768px)
//   #<prefix>-review-week     week label at the top of the expanded body
//   #<prefix>-summary-content the review itself
//   #<prefix>-summary-error   empty state
//   #<prefix>-review-stale    stale notice (8-day rule)
//   #<prefix>-summary-timestamp freshness footer
async function loadSectionReview(prefix, url) {
    const byId = function (suffix) { return document.getElementById(prefix + suffix); };
    const contentEl = byId('-summary-content');
    const errorEl = byId('-summary-error');
    const footerEl = byId('-summary-timestamp');
    const staleEl = byId('-review-stale');

    function showEmpty() {
        contentEl.classList.add('d-none');
        errorEl.classList.remove('d-none');
    }

    try {
        const response = await fetch(url);
        const data = await response.json();
        if (!response.ok || !data.summary || data.error) {
            showEmpty();
            return;
        }

        const structured = data.format === 'structured' && data.structured
            ? buildStructuredReview(data.structured, SECTION_REVIEW_OPTIONS) : null;
        if (structured) {
            contentEl.replaceChildren(structured);
        } else {
            renderSectionMarkdown(contentEl, data.summary);
        }

        applyWeekLabel(byId('-review-week'), formatWeekLabel(data), data);
        applyWeekLabel(byId('-review-range'), formatCompactRange(data), data);
        footerEl.textContent = formatFreshness(data);
        if (staleEl) staleEl.hidden = !isBriefingStale(data);
    } catch (error) {
        console.error('Error loading ' + prefix + ' review:', error);
        showEmpty();
    }
}
