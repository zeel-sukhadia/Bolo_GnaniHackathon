import { useEffect, useRef, useState } from "react";
import { Contact, Me, Meta, TaskSummary, api } from "./api";
import { ErrorNotice, StateTag, VoiceButton, fmtTime, useAction, PhoneInput } from "./ui";

export default function Home({ me, meta }: { me: Me; meta: Meta }) {
  const [request, setRequest] = useState("");
  const [composing, setComposing] = useState(false);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    api.contacts().then(setContacts).catch(() => {});
    api.tasks().then(setTasks).catch((e) => setListError(e.message));
  }, []);

  function heard(t: string) {
    setRequest((r) => (r ? r + " " : "") + t);
    setComposing(true);
  }

  return (
    <>
      <section className="split">
        <div className="plane">
          <p className="eyebrow">You speaks your language,Bolo speaks theirs🗣️✨</p>
          <h1 className="display">Say what you need. I'll make the call.</h1>
        </div>
        <div className="plane plane-red stack" style={{ display: "grid", justifyItems: "center", alignContent: "center", gap: 28 }}>
          <VoiceButton big language={me.language!} onText={heard} enabled={meta.integrations.prisma_stt} label="Speak your request" />
          <button className="btn btn-blue" onClick={() => { setComposing(true); setTimeout(() => textRef.current?.focus(), 0); }}>
            Type instead <span className="arrow" aria-hidden>→</span>
          </button>
        </div>
      </section>

      {composing && <Composer me={me} meta={meta} contacts={contacts} request={request} setRequest={setRequest} textRef={textRef} />}

      <section>
        <div className="plane" style={{ borderBottom: "var(--seam)" }}><h2 className="h2">Your tasks</h2></div>
        <ErrorNotice error={listError} />
        {tasks === null && !listError && <p className="loading">Loading tasks</p>}
        {tasks?.length === 0 && <p className="plane">No tasks yet. Describe your first one above.</p>}
        <div>
          {tasks?.map((t) => (
            <a key={t.id} className="ledger-row" href={`#/task/${t.id}`}>
              <span>
                <span className="who">{t.contact.name || t.contact.phone || "Contact not set"}</span>
                <span className="small" style={{ display: "block" }}>{fmtTime(t.updated_at)}</span>
              </span>
              <span className="detail clamp">{t.goal_for_requester || t.original_request}</span>
              <StateTag state={t.state} />
            </a>
          ))}
        </div>
      </section>
    </>
  );
}

function Composer({ me, meta, contacts, request, setRequest, textRef }: {
  me: Me; meta: Meta; contacts: Contact[]; request: string; setRequest: (s: string) => void;
  textRef: React.RefObject<HTMLTextAreaElement>;
}) {
  const [contactId, setContactId] = useState("");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [lang, setLang] = useState("");
  const { busy, error, run } = useAction();

  return (
    <form className="split split-even" onSubmit={(e) => {
      e.preventDefault();
      void run(async () => {
        const t = await api.createTask({
          request, recipient_language: lang || undefined,
          ...(contactId ? { contact_id: contactId } : { contact_name: name || undefined, contact_phone: phone || undefined }),
        });
        location.hash = `#/task/${t.id}`;
      });
    }}>
      <div className="plane stack">
        <p className="eyebrow">Your request</p>
        <label className="field"><span>What do you need? Check it and fix anything I misheard.</span>
          <textarea ref={textRef} className="input" value={request} onChange={(e) => setRequest(e.target.value)} required minLength={5} maxLength={3000}
            lang={me.language!.slice(0, 2)} /></label>
        <VoiceButton language={me.language!} onText={(t) => setRequest((request ? request + " " : "") + t)} enabled={meta.integrations.prisma_stt} label="Add by voice" />
      </div>
      <div className="plane plane-yellow stack">
        <p className="eyebrow">Who should I call?</p>
        {contacts.length > 0 && (
          <label className="field"><span>Saved contact</span>
            <select className="input" value={contactId} onChange={(e) => setContactId(e.target.value)}>
              <option value="">— New or not sure —</option>
              {contacts.filter((c) => !c.opted_out).map((c) => <option key={c.id} value={c.id}>{c.name} · {c.phone}</option>)}
            </select></label>
        )}
        {!contactId && (
          <div className="grid2">
            <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} /></label>
            <label className="field"><span>Phone</span><PhoneInput value={phone} onChange={setPhone} /></label>
          </div>
        )}
        <label className="field"><span>Their language</span>
          <select className="input" value={lang} onChange={(e) => setLang(e.target.value)}>
            <option value="">Not sure. Work it out, or ask me</option>
            {Object.entries(meta.languages).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
          </select></label>
        <p className="small">You can leave anything blank. I'll only ask for what's essential.</p>
        <button className="btn btn-blue" disabled={busy || request.trim().length < 5}>
          {busy ? "Starting…" : "Start task"} <span className="arrow" aria-hidden>→</span>
        </button>
        {error && <p role="alert" style={{ fontWeight: 650 }}>■ {error}</p>}
      </div>
    </form>
  );
}
