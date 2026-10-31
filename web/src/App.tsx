import { useCallback, useEffect, useState } from "react";
import { ApiError, Me, Meta, Notice, api } from "./api";
import Demo from "./Demo";
import Home from "./Home";
import Settings from "./Settings";
import TaskView from "./TaskView";
import { ErrorNotice, LanguageGrid, fmtTime, useAction } from "./ui";

function useHashRoute() {
  const [hash, setHash] = useState(location.hash.slice(1) || "/");
  useEffect(() => {
    const on = () => { setHash(location.hash.slice(1) || "/"); window.scrollTo(0, 0); };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [bootError, setBootError] = useState<string | null>(null);
  const [unread, setUnread] = useState(0);
  const route = useHashRoute();

  useEffect(() => {
    api.meta().then(setMeta).catch((e) => setBootError(e.message));
    api.me().then(setMe).catch((e) => (e instanceof ApiError && e.status === 401 ? setMe(null) : setBootError(e.message)));
  }, []);

  const pollUnread = useCallback(() => {
    api.notifications().then((n) => setUnread(n.filter((x) => !x.read).length)).catch(() => {});
  }, []);
  useEffect(() => {
    if (!me) return;
    pollUnread();
    const t = setInterval(pollUnread, 15000);
    return () => clearInterval(t);
  }, [me, pollUnread]);

  if (bootError) return <Frame><div className="notice notice-error" role="alert">{bootError} <button className="btn" onClick={() => location.reload()}>Retry</button></div></Frame>;
  if (!meta || me === undefined) return <Frame><p className="loading">Loading</p></Frame>;
  if (me === null) return <Frame><Auth onDone={setMe} /></Frame>;
  if (!me.language) return <Frame><Onboarding me={me} meta={meta} onDone={setMe} /></Frame>;

  if (route === "/demo") return <Demo me={me} meta={meta} />; // full-screen, no app chrome, for recording

  let page;
  if (route.startsWith("/task/")) page = <TaskView id={route.slice(6)} me={me} meta={meta} />;
  else if (route === "/inbox") page = <Inbox onRead={() => setUnread(0)} />;
  else if (route === "/settings") page = <Settings me={me} meta={meta} onChange={setMe} />;
  else page = <Home me={me} meta={meta} />;

  return (
    <Frame nav={<Nav route={route} unread={unread} />} meta={meta}>
      {page}
    </Frame>
  );
}

function Frame({ children, nav, meta }: { children: React.ReactNode; nav?: React.ReactNode; meta?: Meta }) {
  return (
    <div className="shell">
      <header className="top">
        <a className="brand" href="#/" aria-label="Bolo home">
          <span className="brand-mark" aria-hidden><i className="c" /><i className="t" /><i className="s" /></span>
          Bolo
        </a>
        {nav}
      </header>
      {meta?.telephony_mode === "simulated" && (
        <div className="sim-banner" role="note">
          ▲ Simulation mode: calls are simulated. No real phone call is placed; you play the person being called.
          {meta.evon_mode === "mock" && " Evon is in mock mode too: agent replies are canned, not from the model."}
          {meta.evon_mode === "anthropic" && ` Reasoning uses Claude (${meta.llm_model}) as a stand-in for Evon.`}
        </div>
      )}
      <main>{children}</main>
      <footer className="foot">
        <p>The agent always says it's an AI assistant, explains why it's calling, and stops if asked.</p>
        <p>Your task data is kept for a limited time, and you can delete it whenever you want.</p>
      </footer>
    </div>
  );
}

function Nav({ route, unread }: { route: string; unread: number }) {
  const cur = (r: string) => (route === r || (r === "/" && route.startsWith("/task/")) ? "page" : undefined);
  return (
    <nav className="nav" aria-label="Main">
      <a href="#/" aria-current={cur("/")}>Tasks</a>
      <a href="#/inbox" aria-current={cur("/inbox")}>
        Inbox {unread > 0 && <span className="count" aria-label={`${unread} unread`}>{unread}</span>}
      </a>
      <a href="#/settings" aria-current={cur("/settings")}>Settings</a>
      <a href="#/demo">Demo</a>
    </nav>
  );
}

function Auth({ onDone }: { onDone: (m: Me) => void }) {
  const [mode, setMode] = useState<"login" | "register">("register");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const { busy, error, run } = useAction();
  return (
    <section className="split">
      <div className="plane">
        <p className="eyebrow">Multilingual calling agent</p>
        <h1 className="display">You speak your language. Bolo speaks theirs🗣️</h1>
        <p className="lede">Tell it what you need. The agent calls the person in their own language, asks follow-up questions when an answer is vague, and tells you what it learned. It asks before any call you haven't approved.</p>
      </div>
      <form className="plane plane-red stack" onSubmit={(e) => {
        e.preventDefault();
        void run(async () => onDone(mode === "register" ? await api.register(email, password, name) : await api.login(email, password)));
      }}>
        <h2 className="h3">{mode === "register" ? "Create your account" : "Sign in"}</h2>
        {mode === "register" && (
          <label className="field"><span>Your name (the agent says it's calling for you)</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" required maxLength={100} /></label>
        )}
        <label className="field"><span>Email</span>
          <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required /></label>
        <label className="field"><span>Password (8+ characters)</span>
          <input className="input" type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "register" ? "new-password" : "current-password"} required /></label>
        {error && <p role="alert" style={{ fontWeight: 650 }}>■ {error}</p>}
        <div className="row">
          <button className="btn" disabled={busy}>{mode === "register" ? "Create account" : "Sign in"} <span className="arrow" aria-hidden>→</span></button>
          <button type="button" className="btn btn-ghost" onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "New here? Create an account" : "I have an account"}
          </button>
        </div>
      </form>
    </section>
  );
}

function Onboarding({ me, meta, onDone }: { me: Me; meta: Meta; onDone: (m: Me) => void }) {
  const [lang, setLang] = useState<string | null>(null);
  const [approve, setApprove] = useState(true);
  const { busy, error, run } = useAction();
  return (
    <>
      <section className="split">
        <div className="plane stack">
          <p className="eyebrow">Step 1 of 2</p>
          <h1 className="h2">Which language should I use with you?</h1>
          <p className="lede">I'll understand you and report results in this language. You can change it any time.</p>
          <LanguageGrid languages={meta.languages} value={lang} onChange={setLang} />
        </div>
        <div className="plane plane-yellow stack">
          <p className="eyebrow">Step 2 of 2</p>
          <h2 className="h3">Calling permission</h2>
          <label className="check"><input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)} />
            <span>Ask me before every call <span className="small">(recommended). I'll still always ask for new numbers, payments, bookings or personal data.</span></span></label>
          <button className="btn btn-blue" disabled={!lang || busy} onClick={() => void run(async () => onDone(await api.updateMe({
            language: lang, approve_every_call: approve, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          })))}>Continue <span className="arrow" aria-hidden>→</span></button>
          {!lang && <p className="small">Choose a language to continue.</p>}
          <p className="small">Signed in as {me.email}</p>
        </div>
      </section>
      <ErrorNotice error={error} />
    </>
  );
}

function Inbox({ onRead }: { onRead: () => void }) {
  const [items, setItems] = useState<Notice[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.notifications().then((n) => { setItems(n); return api.markRead(); }).then(onRead).catch((e) => setError(e.message));
  }, [onRead]);
  return (
    <>
      <section className="split">
        <div className="plane"><p className="eyebrow">Inbox</p><h1 className="h2">What happened, and what needs you.</h1></div>
        <div className="plane plane-yellow"><p className="small">Calls, answers, approvals and follow-up reminders show up here. Turn on push notifications in Settings to get them on your phone too.</p></div>
      </section>
      <ErrorNotice error={error} />
      {items === null && !error && <p className="loading">Loading</p>}
      {items?.length === 0 && <p className="plane">Nothing yet.</p>}
      <div className="ledger">
        {items?.map((n) => (
          <a key={n.id} className="ledger-row" href={n.task_id ? `#/task/${n.task_id}` : "#/inbox"}>
            <span className="who">{!n.read && <span aria-label="unread">● </span>}{n.title}</span>
            <span className="detail clamp">{n.body}</span>
            <span className="small">{fmtTime(n.created_at)}</span>
          </a>
        ))}
      </div>
    </>
  );
}
