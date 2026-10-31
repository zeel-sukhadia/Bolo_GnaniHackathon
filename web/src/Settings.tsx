import { useEffect, useState } from "react";
import { Contact, Me, Meta, api } from "./api";
import { ErrorNotice, LanguageGrid, useAction, PhoneInput } from "./ui";
import { enablePush, pushSupported } from "./web";

const HOURS = Array.from({ length: 24 }, (_, h) => h);

export default function Settings({ me, meta, onChange }: { me: Me; meta: Meta; onChange: (m: Me) => void }) {
  const { busy, error, run } = useAction();
  const [pushMsg, setPushMsg] = useState<string | null>(null);
  const save = (p: Partial<Me>) => run(async () => onChange(await api.updateMe(p)));
  const quiet = me.quiet_start_hour !== null && me.quiet_end_hour !== null;

  return (
    <>
      <section className="split">
        <div className="plane stack">
          <p className="eyebrow">Settings</p>
          <h1 className="h2">Your language</h1>
          <LanguageGrid languages={meta.languages} value={me.language} onChange={(language) => void save({ language })} />
          <label className="field"><span>Your name (the agent says it's calling on your behalf)</span>
            <input className="input" defaultValue={me.display_name} maxLength={100}
              onBlur={(e) => e.target.value !== me.display_name && void save({ display_name: e.target.value })} /></label>
        </div>
        <div className="plane plane-yellow stack">
          <h2 className="h3">Permissions</h2>
          <label className="check"><input type="checkbox" checked={me.approve_every_call} disabled={busy}
            onChange={(e) => void save({ approve_every_call: e.target.checked })} />
            <span>Ask me before every call</span></label>
          <label className="check"><input type="checkbox" checked={me.allow_auto_redial} disabled={busy}
            onChange={(e) => void save({ allow_auto_redial: e.target.checked })} />
            <span>Redial automatically if a call drops or isn't answered <span className="small">(a few tries, spaced out; only works when "ask before every call" is off)</span></span></label>
          <p className="small">Whatever you choose here, I'll always ask first for new numbers, unclear contacts, payments, bookings and personal data.</p>
          <label className="check"><input type="checkbox" checked={quiet} disabled={busy}
            onChange={(e) => void save(e.target.checked ? { quiet_start_hour: 21, quiet_end_hour: 8 } : { quiet_start_hour: null, quiet_end_hour: null })} />
            <span>Quiet hours: no automatic calls during these hours</span></label>
          {quiet && (
            <div className="grid2">
              <label className="field"><span>From</span>
                <select className="input" value={me.quiet_start_hour!} onChange={(e) => void save({ quiet_start_hour: +e.target.value })}>
                  {HOURS.map((h) => <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>)}</select></label>
              <label className="field"><span>Until</span>
                <select className="input" value={me.quiet_end_hour!} onChange={(e) => void save({ quiet_end_hour: +e.target.value })}>
                  {HOURS.map((h) => <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>)}</select></label>
            </div>
          )}
          <p className="small">Time zone: {me.timezone}</p>
        </div>
      </section>
      <ErrorNotice error={error} />

      <section className="split split-even">
        <Contacts meta={meta} />
        <div className="plane plane-blue on-blue stack">
          <h2 className="h3">Notifications</h2>
          <p className="small">Get alerts when a call ends, an answer comes in, or I need your approval. On iPhone, add this app to your Home Screen first (iOS 16.4 or later).</p>
          {meta.vapid_public_key && pushSupported() ? (
            <button className="btn" onClick={() => {
              setPushMsg(null);
              enablePush(meta.vapid_public_key!).then(api.subscribePush).then(() => setPushMsg("Push notifications are on for this device."))
                .catch((e) => setPushMsg(e.message));
            }}>Turn on push notifications</button>
          ) : (
            <p className="small">{meta.vapid_public_key ? "This browser doesn't support push notifications." : "Push isn't set up on the server yet. You'll still see everything in your Inbox."}</p>
          )}
          {pushMsg && <p className="small" role="status">{pushMsg}</p>}
          <h2 className="h3" style={{ marginTop: 28 }}>Service status</h2>
          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
            <li>Speech recognition (Prisma v2.5): {meta.integrations.prisma_stt ? "set up" : "not set up"}</li>
            <li>Voice (Timbre v2.5): {meta.integrations.timbre_tts ? "set up" : "not set up"}</li>
            <li>Reasoning Model: {meta.evon_mode === "mock" ? "mock (simulated replies, no model)" : meta.evon_mode === "anthropic" ?  "Evon v3.3" : "Evon v3.3"} <EvonCheck /></li>
            <li>Phone calls: {meta.telephony_mode === "simulated" ? "simulated (no real calls)" : meta.telephony_mode}</li>
          </ul>
          <button className="btn" onClick={() => void api.logout().then(() => location.reload())}>Sign out</button>
        </div>
      </section>
    </>
  );
}

function EvonCheck() {
  const [msg, setMsg] = useState<string | null>(null);
  const { busy, run } = useAction();
  return (
    <>
      <button type="button" className="btn btn-ghost" style={{ minHeight: 0, padding: "0 4px" }} disabled={busy} onClick={() => void run(async () => {
        try {
          const h = await api.evonHealth();
          setMsg(h.model_available ? `reachable${h.latency_ms != null ? ` (${h.latency_ms} ms)` : ""}` : `unavailable: ${h.detail}`);
        } catch (e) { setMsg(e instanceof Error ? e.message : "check failed"); }
      })}>{busy ? "Checking…" : "Check now"}</button>
      {msg && <span role="status"> {msg}</span>}
    </>
  );
}

function Contacts({ meta }: { meta: Meta }) {
  const [list, setList] = useState<Contact[] | null>(null);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [lang, setLang] = useState("");
  const { busy, error, run } = useAction();
  useEffect(() => { api.contacts().then(setList).catch(() => setList([])); }, []);

  return (
    <div className="plane stack">
      <h2 className="h3">Saved contacts</h2>
      <p className="small">Calls to saved contacts don't need the "new number" check.</p>
      {list?.length === 0 && <p className="small">No contacts yet.</p>}
      <div className="ledger">
        {list?.map((c) => (
          <div key={c.id} className="ledger-row" style={{ paddingInline: 0 }}>
            <span className="who">{c.name}</span>
            <span className="detail small">{c.phone}{c.language ? ` · ${meta.languages[c.language]}` : ""}{c.opted_out ? " · asked not to be called" : ""}</span>
            <button className="btn btn-ghost" aria-label={`Delete ${c.name}`} disabled={busy}
              onClick={() => void run(async () => { await api.deleteContact(c.id); setList(list.filter((x) => x.id !== c.id)); })}>Delete</button>
          </div>
        ))}
      </div>
      <form className="stack" onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          const c = await api.addContact({ name, phone, language: lang || null });
          setList([...(list ?? []), c]); setName(""); setPhone(""); setLang("");
        });
      }}>
        <div className="grid2">
          <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} /></label>
          <label className="field"><span>Phone</span><PhoneInput value={phone} onChange={setPhone} required /></label>
        </div>
        <label className="field"><span>Their language (optional)</span>
          <select className="input" value={lang} onChange={(e) => setLang(e.target.value)}>
            <option value="">Unknown</option>
            {Object.entries(meta.languages).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
          </select></label>
        <button className="btn" disabled={busy}>Add contact <span className="arrow" aria-hidden>+</span></button>
        <ErrorNotice error={error} />
      </form>
    </div>
  );
}
