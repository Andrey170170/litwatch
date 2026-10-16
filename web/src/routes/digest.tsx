/**
 * The Digest tab (DESIGN.md, "Digest"): Day, Week and History, each a route of its own, and a digest in full. On the
 * desktop the three are a segmented control and a digest slides over the view (`?digest=<id>` on the view's route,
 * so Esc, the shade and Close put the view back as it was); on the phone they are the drawer's items under Digest,
 * the view's controls sit in the bottom bar, and a digest is a page of its own (`/digests/<id>`).
 */
import { useInfiniteQuery, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Link, useNavigate, useSearch, type LinkProps } from "@tanstack/react-router";
import { Fragment, useEffect, type CSSProperties, type MouseEvent, type ReactNode } from "react";
import { ApiError, type Schemas } from "@/api/client";
import { useWriteAgain } from "@/api/mutations";
import { dayQuery, digestQuery, historyQuery, summaryQuery, weekQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Sheet } from "@/components/Sheet";
import { Sentinel } from "@/components/Shelves";
import { Back } from "@/components/Shell";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { addDays, calendarMonths, dayLabel, isoDay, listRows, markLine, markShort, mondayOf, monthLabel, periodLine, sections, slotted, SPAN, weekRange, type Card, type DigestPaper, type Mark } from "@/lib/digests";
import { openedFrom } from "@/lib/listContext";
import { rowDate } from "@/lib/format";
import { rc } from "@/lib/rarity";
import { Failed, Load } from "./settings/parts";

const today = () => isoDay(new Date());
const stop = (e: MouseEvent) => e.stopPropagation();

// -- the views' chrome ---------------------------------------------------------------------------------------------

type View = "day" | "week" | "history";
const VIEWS: { view: View; to: "/" | "/week" | "/history"; name: string }[] = [
  { view: "day", to: "/", name: "Day" },
  { view: "week", to: "/week", name: "Week" },
  { view: "history", to: "/history", name: "History" },
];

/** Day · Week · History on the desktop, and History's List · Calendar at the right. */
function Views({ view, children }: { view: View; children?: ReactNode }) {
  return (
    <div className="segbar">
      <nav className="vseg" aria-label="Digest views">
        {VIEWS.map((v) => (
          <Link key={v.view} to={v.to} className={`vs${v.view === view ? " on" : ""}`} aria-current={v.view === view ? "page" : undefined}>
            {v.name}
          </Link>
        ))}
      </nav>
      <span className="sp" />
      {children}
    </div>
  );
}

/** ‹ label ›: `prev` is older, `next` newer; none disables its arrow. On the desktop a strip above the cards; on
 * the phone the bottom bar, with large arrows (`foot`). A step replaces the entry: Back leaves the tab, not a trail
 * of days. */
function Stepper({ to, prev, next, label, small, foot }: { to: "/" | "/week"; prev: string | null | undefined; next: string | null | undefined; label: string; small?: string; foot?: boolean }) {
  const param = to === "/" ? "day" : "week";
  const arrow = (day: string | null | undefined, glyph: string, name: string) =>
    day ? (
      <Link to={to} search={{ [param]: day }} replace className="ib" aria-label={name}>
        {glyph}
      </Link>
    ) : (
      <span className="ib" aria-disabled="true" aria-label={name}>
        {glyph}
      </span>
    );
  return (
    <div className={foot ? "stp foot" : "stp"}>
      {arrow(prev, "‹", "Older")}
      <span className="lbl">
        {small && <small>{small}</small>}
        {label}
      </span>
      {arrow(next, "›", "Newer")}
    </div>
  );
}

/** Where a digest opens from a view: the slide-over on the desktop (a search param on the view, replaced so the
 * view's Back is kept, and the view keeps its scroll under the slide-over), the digest's page on the phone. */
function useOpenDigest(): (id: number) => LinkProps {
  const desktop = useDesktop();
  if (!desktop) return (id) => ({ to: "/digests/$id", params: { id: String(id) } });
  return (id) => ({ to: ".", search: (s: Record<string, unknown>) => ({ ...s, digest: id }), replace: true, resetScroll: false }) as LinkProps;
}

// -- the cards -----------------------------------------------------------------------------------------------------

const rarityStyle = (r: Schemas["Rarity"] | null | undefined) => ({ "--rc": rc(r) }) as CSSProperties;
const countStyle = (m: Mark) => ({ color: m.state === "written" && m.best_rarity ? rc(m.best_rarity) : undefined });

type List = { key: string; ids: string[]; total: number };

/** A paper as a digest lists it: the stripe and glow, the title, the date; with `gist`, its Gist and a pick's reason
 * under them. Set aside since, it is struck through (digests.md). The row carries the list it is in, so the paper's
 * page knows its place (`listContext`). */
function PaperRow({ p, topic, gist, list }: { p: DigestPaper; topic: string; gist?: boolean; list: List }) {
  const onClick = (e: MouseEvent) => {
    stop(e);
    openedFrom(list.key, list.ids, list.total);
  };
  return (
    <Link to="/paper/$" params={{ _splat: p.id }} search={{ topic }} state={{ list: list.key }} className={`row dpr${gist ? " g" : ""}${p.set_aside ? " aside" : ""}`} style={rarityStyle(p.rarity)} onClick={onClick} title={p.set_aside ? "Set aside since" : undefined}>
      <span className="ti">{p.title}</span>
      <span className="dt">{rowDate(p.announced)}</span>
      {gist && p.gist && <span className="gs">{p.gist}</span>}
      {gist && p.why && <span className="wy">{p.why}</span>}
    </Link>
  );
}

/** A digest at Gist level: the head with the count in the best rarity's colour, the Gist, up to three papers, the
 * rest as a count; the whole card opens the digest. Quiet is the head alone; failed, one red line. */
function DigestCard({ card }: { card: Card }) {
  const open = useOpenDigest();
  const navigate = useNavigate();
  const go = () => void navigate(open(card.id));
  if (card.state === "failed")
    return (
      <div className="dblock failed" onClick={go}>
        <Badge topic={card.topic} />
        <span>
          <Link {...open(card.id)} className="n" onClick={stop}>
            {card.topic.name}
          </Link>
          : this digest could not be written. The next run writes it again.
        </span>
      </div>
    );
  const quiet = card.state === "nothing_new";
  return (
    <div className={`dblock${quiet ? " quiet" : ""}`} onClick={go}>
      <div className="hd">
        <Badge topic={card.topic} />
        <Link {...open(card.id)} className="n" onClick={stop}>
          {card.topic.name}
        </Link>
        <span className="c" style={countStyle(card)}>
          {markLine(card)}
        </span>
      </div>
      {!quiet && (
        <>
          {card.gist && <p className="p">{card.gist}</p>}
          {card.papers.length > 0 && (
            <div className="dlist">
              {card.papers.map((p) => (
                <PaperRow key={p.id} p={p} topic={card.topic.slug} list={{ key: `digest:${card.id}`, ids: card.papers.map((x) => x.id), total: card.papers.length + card.more }} />
              ))}
            </div>
          )}
          {card.more > 0 && <div className="more">+{card.more} papers</div>}
        </>
      )}
    </div>
  );
}

/** A Mark as History lists it: the badge, the name, the count at the right, the stripe and glow. */
function MarkRow({ m }: { m: Mark }) {
  const open = useOpenDigest();
  const failed = m.state === "failed";
  return (
    <Link {...open(m.id)} className={`row drow${m.state === "nothing_new" ? " quiet" : ""}${failed ? " bad" : ""}`} style={rarityStyle(m.best_rarity)}>
      <Badge topic={m.topic} />
      <span className="nm">{m.topic.name}</span>
      <span className="c" style={countStyle(m)}>
        {markLine(m)}
      </span>
    </Link>
  );
}

const Marks = ({ marks }: { marks: Mark[] }) => (
  <div className="dlist">
    {marks.map((m) => (
      <MarkRow key={m.id} m={m} />
    ))}
  </div>
);

// -- Day and Week --------------------------------------------------------------------------------------------------

/** The view's load: "Loading…", a quiet "Updating…" behind cached cards, or when it never loaded, which read failed
 * and the way to try again. */
function State({ q, what }: { q: Pick<UseQueryResult, "isError" | "isPending" | "isFetching" | "refetch" | "data">; what: string }) {
  return q.isError && q.data === undefined ? <Failed q={q} what={what} /> : <Load q={q} />;
}

export function DigestPage() {
  const search = useSearch({ from: "/" });
  const day = search.day ?? today();
  const q = useQuery(dayQuery(day));
  const desktop = useDesktop();
  const stepper = (foot?: boolean) => <Stepper to="/" prev={q.data?.previous} next={q.data?.next} label={dayLabel(day)} small={day === today() ? "Today" : undefined} foot={foot} />;
  return (
    <>
      <Fill name="bar">
        <span className="t">Digest</span>
      </Fill>
      <Fill name="foot">
        <div className="pfoot">{stepper(true)}</div>
      </Fill>
      {desktop && (
        <>
          <Views view="day" />
          {stepper()}
        </>
      )}
      {q.data?.weekly && (
        <Link to="/week" search={{ week: q.data.weekly }} className="wkline">
          Weekly digests for {weekRange(q.data.weekly)} are out <span className="ml-auto">›</span>
        </Link>
      )}
      <State q={q} what="the day" />
      {q.data && !q.data.digests.length && <div className="none">{q.data.released ? "No digests for this day." : "No papers released."}</div>}
      {q.data?.digests.map((c) => (
        <DigestCard key={c.id} card={c} />
      ))}
      <SlideOver id={search.digest} />
    </>
  );
}

export function WeekPage() {
  const search = useSearch({ from: "/week" });
  // Without a week: last week's, written this Monday.
  const last = addDays(mondayOf(today()), -7);
  const week = search.week ?? last;
  const q = useQuery(weekQuery(week));
  const desktop = useDesktop();
  const monday = q.data?.monday ?? mondayOf(week);
  const stepper = (foot?: boolean) => <Stepper to="/week" prev={q.data?.previous} next={q.data?.next} label={weekRange(monday)} small={monday === last ? "Last week" : undefined} foot={foot} />;
  return (
    <>
      <Fill name="bar">
        <span className="t">Digest · Week</span>
      </Fill>
      <Fill name="foot">
        <div className="pfoot">{stepper(true)}</div>
      </Fill>
      {desktop && (
        <>
          <Views view="week" />
          {stepper()}
        </>
      )}
      {q.data?.digests.length ? <div className="weeksub">Written {dayLabel(addDays(monday, 7))}</div> : null}
      <State q={q} what="the week" />
      {q.data && !q.data.digests.length && <div className="none">No weekly digests for this week.</div>}
      {q.data?.digests.map((c) => (
        <DigestCard key={c.id} card={c} />
      ))}
      <SlideOver id={search.digest} />
    </>
  );
}

// -- History -------------------------------------------------------------------------------------------------------

/** History's two forms; on the phone in the bottom bar, with their names. */
function Modes({ cal, foot }: { cal: boolean; foot?: boolean }) {
  return (
    <nav className={`modes${foot ? " foot" : ""}`} aria-label="History as">
      <Link to="/history" search={{}} replace className={cal ? "" : "on"} aria-current={cal ? undefined : "page"} aria-label="List">
        ☰{foot && <span>List</span>}
      </Link>
      <Link to="/history" search={{ view: "cal" }} replace className={cal ? "on" : ""} aria-current={cal ? "page" : undefined} aria-label="Calendar">
        ▦{foot && <span>Calendar</span>}
      </Link>
    </nav>
  );
}

/** A group's head in the list: the label opens its Day or Week. */
function Group({ to, label, sub, pin }: { to?: LinkProps; label: string; sub?: string; pin?: boolean }) {
  return (
    <div className={`grp${pin ? " pin" : ""}`}>
      {to ? (
        <Link {...to} className="t">
          {label}
        </Link>
      ) : (
        <span className="t quiet">{label}</span>
      )}
      {sub && <span className="s">{sub}</span>}
    </div>
  );
}

function HistoryList({ pages }: { pages: Schemas["History"][] }) {
  return listRows(pages, today()).map((r) => {
    switch (r.kind) {
      case "pin":
        return (
          <Fragment key="pin">
            <Group to={{ to: "/week", search: { week: r.monday } }} label="This week" sub={`weekly digests · ${weekRange(r.monday)}`} pin />
            <Marks marks={r.marks} />
          </Fragment>
        );
      case "week":
        return (
          <Fragment key={`w${r.monday}`}>
            <Group to={{ to: "/week", search: { week: r.monday } }} label={`Week ${weekRange(r.monday)}`} sub="weekly digests" pin />
            <Marks marks={r.marks} />
          </Fragment>
        );
      case "quiet":
        return <Group key={r.to} label={r.from === r.to ? dayLabel(r.to) : `${dayLabel(r.from).slice(0, 3)} – ${dayLabel(r.to)}`} sub="No papers released" />;
      case "day":
        return (
          <Fragment key={r.day}>
            <Group to={{ to: "/", search: { day: r.day } }} label={dayLabel(r.day)} sub={r.today ? "today" : undefined} />
            {r.marks.length ? <Marks marks={r.marks} /> : <div className="none">No digests.</div>}
          </Fragment>
        );
    }
  });
}

/** A Mark as a bar in a calendar cell: colour only on the phone; its badge and count on the desktop, where it opens
 * the digest. */
function Bar({ m, text }: { m: Mark; text: boolean }) {
  const open = useOpenDigest();
  const cls = `mbar${m.state === "nothing_new" ? " h" : m.state === "failed" ? " x" : ""}`;
  const title = `${m.topic.name} · ${markLine(m)}`;
  if (!text) return <span className={cls} style={rarityStyle(m.best_rarity)} title={title} />;
  return (
    <Link {...open(m.id)} className={cls} style={rarityStyle(m.best_rarity)} title={title} onClick={stop}>
      <Badge topic={m.topic} size="sm" />
      <span className="mc">{markShort(m)}</span>
    </Link>
  );
}

/** History as months, newest first, a row per week: Monday to Friday, then the week's weeklies in two columns, Focus
 * and Area level with the days' bars and Field beside them. */
function Calendar({ pages }: { pages: Schemas["History"][] }) {
  const desktop = useDesktop();
  const navigate = useNavigate();
  const now = today();
  // The topics' fixed order, from the summary the shell reads; a day's bars and the W cell's columns keep a slot per
  // topic, empty where it has no digest, so a topic is always in the same place (digests.md).
  const topics = useQuery(summaryQuery).data?.topics ?? [];
  const daily = topics.filter((t) => t.depth !== "field").map((t) => t.slug);
  const weekly = topics.filter((t) => t.depth === "field").map((t) => t.slug);
  const bars = (marks: Mark[], order: string[]) => slotted(marks, order).map((m, i) => (m ? <Bar key={m.id} m={m} text={desktop} /> : <span key={`-${i}`} className="mbar none" />));
  // On the phone the bars are too thin to tap: a tap anywhere in the cell opens its Day or Week (the date is the link
  // the keyboard reaches).
  const cell = (to: LinkProps) => (desktop ? {} : { onClick: () => void navigate(to) });
  return calendarMonths(pages).map((mo) => (
    <section key={mo.month} className="calmonth" aria-label={monthLabel(`${mo.month}-01`)}>
      <div className="sec">{monthLabel(`${mo.month}-01`)}</div>
      <div className={`month${desktop ? " text" : ""}`}>
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Week"].map((n) => (
          <div key={n} className="mh">
            {n}
          </div>
        ))}
        {mo.weeks.map((w, wi) => {
          const weekTo: LinkProps = { to: "/week", search: { week: w.monday } };
          return (
            <Fragment key={w.monday}>
              {w.days.map((d, i) => {
                const day = addDays(w.monday, i);
                if (!d) return <div key={day} className="mday empty" />;
                const dayTo: LinkProps = { to: "/", search: { day } };
                const num = Number(day.slice(8));
                return (
                  <div key={day} className={`mday${day === now ? " today" : ""}${d.released || d.marks.length ? "" : " off"}`} {...cell(dayTo)}>
                    <Link {...dayTo} className="mnum" onClick={stop}>
                      {num === 1 || (wi === 0 && i === 0) ? `${num} ${monthLabel(day).slice(0, 3)}` : num}
                    </Link>
                    {bars(d.marks, daily)}
                  </div>
                );
              })}
              {w.weekly.length ? (
                <div className="mweek" {...cell(weekTo)}>
                  <Link {...weekTo} className="mnum" onClick={stop}>
                    W {weekRange(w.monday)}
                  </Link>
                  <div className="wcols">
                    <div>{bars(w.weekly.filter((m) => m.topic.depth !== "field"), daily)}</div>
                    <div>{bars(w.weekly.filter((m) => m.topic.depth === "field"), weekly)}</div>
                  </div>
                </div>
              ) : (
                <div className="mweek empty">
                  <span className="mnum">W</span>
                </div>
              )}
            </Fragment>
          );
        })}
      </div>
    </section>
  ));
}

export function HistoryPage() {
  const search = useSearch({ from: "/history" });
  const cal = search.view === "cal";
  const desktop = useDesktop();
  const q = useInfiniteQuery(historyQuery(today(), SPAN));
  const pages = q.data?.pages ?? [];
  const start = pages[0]?.start;
  return (
    <>
      <Fill name="bar">
        <span className="t">Digest · History</span>
      </Fill>
      <Fill name="foot">
        <div className="pfoot">
          <Modes cal={cal} foot />
        </div>
      </Fill>
      {desktop && (
        <Views view="history">
          <Modes cal={cal} />
        </Views>
      )}
      {!q.isFetchNextPageError && <State q={q} what="the history" />}
      {pages.length > 0 && !start && <div className="empty">No digests have been written yet.</div>}
      {start && (cal ? <Calendar pages={pages} /> : <HistoryList pages={pages} />)}
      {q.hasNextPage && !q.isFetchingNextPage && !q.isError && <Sentinel onSeen={() => void q.fetchNextPage()} />}
      {q.isFetchingNextPage && <div className="loadmore">Loading older weeks…</div>}
      {q.isFetchNextPageError && (
        <div className="loadmore">
          Could not load older weeks ·{" "}
          <button className="inl" onClick={() => void q.fetchNextPage()}>
            Try again
          </button>
        </div>
      )}
      {start && !q.hasNextPage && <div className="archstart">The archive starts on {dayLabel(start)}.</div>}
      <SlideOver id={search.digest} />
    </>
  );
}

// -- a digest ------------------------------------------------------------------------------------------------------

/** A digest in full: the head, the text, then its papers by role. A failed one offers Write again; the answer lands
 * in the digest's cache (`useWriteAgain`). Keyed by id where it is placed, so one digest's pending write is not
 * another's. */
export function DigestView({ id }: { id: number }) {
  const q = useQuery(digestQuery(id));
  const write = useWriteAgain();
  const d = q.data;
  if (!d) return q.isError ? q.error instanceof ApiError && q.error.status === 404 ? <div className="empty">No such digest.</div> : <Failed q={q} what="the digest" /> : <Load q={q} />;
  const secs = sections(d.topic.depth, d.period, d.papers);
  const ids = secs.flatMap((s) => s.papers.map((p) => p.id));
  const list = { key: `digest:${d.id}:full`, ids, total: ids.length };
  return (
    <div className="dgfull" style={rarityStyle(d.best_rarity)}>
      <State q={q} what="the digest" />
      <div className="dghead">
        <Badge topic={d.topic} size="lg" />
        <div>
          <div className="h1">
            <Link to="/topics/$slug" params={{ slug: d.topic.slug }}>
              {d.topic.name}
            </Link>
          </div>
          <div className="sub">
            {periodLine(d)} · <span style={countStyle(d)}>{markLine(d)}</span>
          </div>
        </div>
      </div>
      {d.state === "failed" && (
        <div className="note fail">
          <div>
            <b>This digest could not be written.</b> Its papers stay due, and the next run writes it again.
            {d.error && <div className="err">{d.error}</div>}
          </div>
          <div className="acts">
            <button className="btn" disabled={write.isPending} onClick={() => write.mutate(d.id)}>
              {write.isPending ? "Writing…" : "Write again"}
            </button>
          </div>
        </div>
      )}
      {d.state === "nothing_new" && <p className="prose faint">Nothing was due for this topic. The run looked and found nothing new.</p>}
      {d.text?.trim() &&
        d.text.split(/\n\s*\n/).map((p, i) => (
          <p key={i} className="prose">
            {p}
          </p>
        ))}
      {secs.map((s) => (
        <Fragment key={s.name}>
          <div className="cap">{s.name}</div>
          <div className="dlist">
            {s.papers.map((p) => (
              <PaperRow key={p.id} p={p} topic={d.topic.slug} gist={s.gist} list={list} />
            ))}
          </div>
        </Fragment>
      ))}
    </div>
  );
}

/** The digest's own page: the phone's always, the desktop's when opened from outside the Digest tab (a topic's
 * strip). */
export function DigestOnePage({ id }: { id: number }) {
  const q = useQuery(digestQuery(id));
  return (
    <>
      <Fill name="title">
        {q.data && <Badge topic={q.data.topic} />}
        <span className="t">{q.data ? `${q.data.topic.name}${q.data.period === "week" ? " · week" : ""}` : "Digest"}</span>
      </Fill>
      <Back />
      <DigestView key={id} id={id} />
    </>
  );
}

/** The slide-over on the desktop: the digest over the view, closed by the shade, Esc or "‹ Close", which take the
 * search param off (replaced, so Back still leaves the tab). Opening it rereads the digest behind what is cached,
 * as a visit to a page does. The phone has no slide-over: a view's URL naming a digest goes on to the digest's page,
 * the view beneath it. */
function SlideOver({ id }: { id: number | undefined }) {
  const desktop = useDesktop();
  const navigate = useNavigate();
  const qc = useQueryClient();
  useEffect(() => {
    if (id === undefined) return;
    if (desktop) void qc.invalidateQueries({ queryKey: ["digest", String(id)] });
    else {
      // the view stays under the digest's page, without the param, so Back lands on it and not on this redirect
      void navigate({ to: ".", search: (s: Record<string, unknown>) => ({ ...s, digest: undefined }), replace: true } as never).then(() => navigate({ to: "/digests/$id", params: { id: String(id) } }));
    }
  }, [id, desktop, qc, navigate]);
  if (!desktop || id === undefined) return null;
  const close = () => void navigate({ to: ".", search: (s: Record<string, unknown>) => ({ ...s, digest: undefined }), replace: true, resetScroll: false } as never);
  return (
    <Sheet open onClose={close} side="right" label="Digest">
      <button className="back" onClick={close}>
        ‹ Close
      </button>
      <DigestView key={id} id={id} />
    </Sheet>
  );
}
