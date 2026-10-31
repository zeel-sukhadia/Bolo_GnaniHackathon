import { useCallback, useEffect, useState } from "react";
import { Approval, Contact, FACT_LABEL, LIVE_STATES, Me, Meta, Task, TaskState, api } from "./api";
import { ErrorNotice, ListenButton, StateTag, VoiceButton, fmtTime, useAction, PhoneInput } from "./ui";

const STEPS = ["Plan", "Approve", "Call", "Verify", "Result"];
const STEP_OF: Record<TaskState, number> = {
  NEW: 0, PLANNING: 0, AWAITING_INPUT: 0, AWAITING_APPROVAL: 1, CALLING: 2, CLARIFYING: 2, WAITING_RETRY: 2,
  VERIFYING: 3, WAITING_FOLLOW_UP: 4, COMPLETED: 4, NEEDS_HUMAN_HELP: -1, FAILED: -1, CANCELLED: -1,
};

export default function TaskView({ id, me, meta }: { id: string; me: Me; meta: Meta }) {
  const [task, setTask] = useState<Task | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const act = useAction();

  const load = useCallback(() => api.task(id).then((t) => { setTask(t); setLoadError(null); }).catch((e) => setLoadError(e.message)), [id]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!task) return;
    const t = setInterval(load, LIVE_STATES.includes(task.state) ? 2500 : 10000);
    return () => clearInterval(t);
  }, [task?.state, load]);

  if (!task) return loadError ? <ErrorNotice error={loadError} /> : <p className="loading">Loading task</p>;
  const doit = (fn: () => Promise<Task>) => act.run(async () => setTask(await fn()));
  const terminal = ["COMPLETED", "FAILED", "CANCELLED"].includes(task.state);
  const lang = (c: string | null) => (c ? meta.languages[c] ?? c : "Unknown");
  const step = STEP_OF[task.state];

  return (
    <>
      <section className="split">
        <div className="plane plane-yellow stack">
          <div className="row"><StateTag state={task.state} />{task.active_call && <span className="state" data-tone="live">● Simulated call</span>}</div>
          <h1 className="h2">{task.goal_for_requester || task.original_request}</h1>
          {task.goal_for_requester && <p className="small">You said: “{task.original_request}”</p>}
        </div>
        <div className="plane stack">
          <dl className="stack" style={{ margin: 0 }}>
            <div><dt className="label">Calling</dt><dd style={{ margin: 0 }}>{task.contact.name || "—"} {task.contact.phone && <span className="small">· {task.contact.phone}</span>}
              {task.contact.status !== "confirmed" && <span className="small"> ({task.contact.status === "unconfirmed" ? "new number" : task.contact.status})</span>}</dd></div>
            <div><dt className="label">Languages</dt><dd style={{ margin: 0 }}>You: {lang(task.requester_language)} · Them: {lang(task.recipient_language)}</dd></div>
            {task.next_action && <div><dt className="label">Next</dt><dd style={{ margin: 0 }}>{task.next_action}</dd></div>}
          </dl>
        </div>
      </section>

      <ol className="steps" aria-label="Progress">
        {STEPS.map((s, i) => (
          <li key={s} className="step" data-at={step < 0 ? "todo" : i < step ? "done" : i === step ? "now" : "todo"}
            aria-current={i === step ? "step" : undefined} style={{ listStyle: "none" }}>
            <div className="n">{i + 1}</div><div className="t">{s}</div>
          </li>
        ))}
      </ol>

      {task.status_detail && <div className={`notice ${["NEEDS_HUMAN_HELP", "FAILED", "WAITING_RETRY"].includes(task.state) ? "notice-error" : "notice-info"}`} role="status">{task.status_detail}</div>}
      <ErrorNotice error={act.error} />

      {task.state === "PLANNING" && <p className="loading" role="status">Working out what to ask…</p>}
      {task.state === "AWAITING_INPUT" && <InputsPanel task={task} me={me} meta={meta} busy={act.busy} onSubmit={(b) => doit(() => api.inputs(task.id, b))} />}
      {task.state === "AWAITING_APPROVAL" && <ApprovalPanel task={task} meta={meta} busy={act.busy}
        onDecide={(a, b) => doit(() => api.decide(task.id, a.id, b))} />}
      {task.active_call && <SimConsole task={task} meta={meta} busy={act.busy} doit={doit} />}
      {task.state === "WAITING_RETRY" && (
        <section className="plane plane-red stack">
          <h2 className="h3">The call didn't go through.</h2>
          <p>Answers already collected are saved, and the next call picks up from there.</p>
          <div className="row"><button className="btn" disabled={act.busy} onClick={() => void doit(() => api.retry(task.id))}>Call again <span className="arrow" aria-hidden>→</span></button></div>
        </section>
      )}
      {task.state === "WAITING_FOLLOW_UP" && <FollowUpPanel task={task} busy={act.busy} doit={doit} />}
      {task.state === "NEEDS_HUMAN_HELP" && <HelpPanel task={task} me={me} meta={meta} busy={act.busy} doit={doit} />}

      {task.result_summary && (
        <section className="split">
          <div className="plane stack">
            <p className="eyebrow">Result, in {lang(task.requester_language)}</p>
            <p style={{ fontSize: 22, fontWeight: 650, lineHeight: 1.4 }} lang={task.requester_language.slice(0, 2)}>{task.result_summary}</p>
            <ListenButton url={api.summaryAudioUrl(task.id)} enabled={meta.integrations.timbre_tts} />
          </div>
          <div className="plane plane-quiet small">Facts marked <b>Confirmed</b> were stated clearly and checked back with them. <b>Estimates</b> are approximate. <b>Not answered</b> means they couldn't or wouldn't say. Nothing is guessed.</div>
        </section>
      )}

      {task.required_info.length > 0 && (
        <section>
          <div className="plane" style={{ borderTop: "var(--seam)", borderBottom: "var(--rule)" }}><h2 className="h3">What I need to find out</h2></div>
          <div className="facts">
            {task.required_info.map((f) => (
              <div key={f.key} className="fact" data-status={f.status}>
                <div className="status">{FACT_LABEL[f.status] ?? f.status}</div>
                <div className="small" style={{ marginTop: 8 }}>{f.description}</div>
                {f.value && <div className="value">{f.value}</div>}
                {f.evidence && <div className="evidence">They said: “{f.evidence}”</div>}
              </div>
            ))}
          </div>
        </section>
      )}

      <Transcript task={task} meta={meta} />

      <section className="plane stack" style={{ borderTop: "var(--seam)" }}>
        <div className="row">
          {!terminal && task.state !== "NEEDS_HUMAN_HELP" && <button className="btn" disabled={act.busy} onClick={() => void doit(() => api.takeover(task.id))}>I'll handle it myself</button>}
          {!terminal && <button className="btn btn-red" disabled={act.busy} onClick={() => void doit(() => api.cancel(task.id))}>Cancel task</button>}
          {!task.active_call && <DeleteButton id={task.id} />}
        </div>
        <details>
          <summary className="label" style={{ cursor: "pointer" }}>Activity log</summary>
          <ul className="small" style={{ paddingLeft: 18 }}>
            {task.events.map((e, i) => <li key={i}>{fmtTime(e.at)} · {e.actor === me.id ? "you" : e.actor} · {e.kind}{e.detail?.to ? ` → ${e.detail.to}` : ""}{e.detail?.decision ? `: ${e.detail.decision}` : ""}</li>)}
          </ul>
        </details>
      </section>
    </>
  );
}

function InputsPanel({ task, me, meta, busy, onSubmit }: {
  task: Task; me: Me; meta: Meta; busy: boolean;
  onSubmit: (b: { note?: string; contact_id?: string; contact_name?: string; contact_phone?: string; recipient_language?: string }) => void;
}) {
  const [note, setNote] = useState("");
  const [phone, setPhone] = useState("");
  const [name, setName] = useState(task.contact.name ?? "");
  const [contactId, setContactId] = useState("");
  const [rlang, setRlang] = useState("");
  const [contacts, setContacts] = useState<Contact[]>([]);
  const needPhone = task.missing_inputs.includes("contact_phone");
  const needChoice = task.missing_inputs.includes("contact_choice");
  const needLang = task.missing_inputs.includes("recipient_language");
  const questions = task.missing_inputs.filter((q) => !["contact_phone", "contact_choice", "recipient_language"].includes(q));
  useEffect(() => { if (needChoice) api.contacts().then(setContacts).catch(() => {}); }, [needChoice]);
  const matches = contacts.filter((c) => c.name.toLowerCase() === (task.contact.name ?? "").toLowerCase());

  return (
    <form className="split" onSubmit={(e) => {
      e.preventDefault();
      onSubmit({ note: note || undefined, recipient_language: rlang || undefined,
        ...(contactId ? { contact_id: contactId } : phone ? { contact_phone: phone, contact_name: name || undefined } : {}) });
    }}>
      <div className="plane stack">
        <h2 className="h3">I need a little more before I can call.</h2>
        {needChoice && (
          <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="label">Several contacts are called “{task.contact.name}”. Which one?</legend>
            {matches.map((c) => (
              <label key={c.id} className="check"><input type="radio" name="contact" value={c.id} checked={contactId === c.id} onChange={() => setContactId(c.id)} />
                <span>{c.name} · {c.phone}</span></label>
            ))}
          </fieldset>
        )}
        {needPhone && (
          <div className="grid2">
            <label className="field"><span>Their name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field"><span>Their phone number</span><PhoneInput value={phone} onChange={setPhone} required /></label>
          </div>
        )}
        {needLang && (
          <label className="field"><span>Which language should I speak with them?</span>
            <select className="input" required value={rlang} onChange={(e) => setRlang(e.target.value)}>
              <option value="">Choose…</option>
              {Object.entries(meta.languages).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
            </select></label>
        )}
        {questions.length > 0 && (
          <>
            <ul style={{ margin: 0, paddingLeft: 20 }}>{questions.map((q) => <li key={q} lang={task.requester_language.slice(0, 2)}>{q}</li>)}</ul>
            <label className="field"><span>Your answer</span><textarea className="input" value={note} onChange={(e) => setNote(e.target.value)} required /></label>
            <VoiceButton language={me.language!} onText={(t) => setNote((n) => (n ? n + " " : "") + t)} enabled={meta.integrations.prisma_stt} label="Answer by voice" />
          </>
        )}
      </div>
      <div className="plane plane-yellow stack" style={{ alignContent: "end" }}>
        <p className="small">I only ask for details I can't proceed without.</p>
        <button className="btn btn-blue" disabled={busy || (needChoice && !contactId)}>Continue <span className="arrow" aria-hidden>→</span></button>
      </div>
    </form>
  );
}

function ApprovalPanel({ task, meta, busy, onDecide }: {
  task: Task; meta: Meta; busy: boolean;
  onDecide: (a: Approval, b: { decision: "approve" | "reject" | "edit"; instruction?: string; contact_phone?: string }) => void;
}) {
  const a = task.approvals.find((x) => x.status === "pending");
  const [editing, setEditing] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [phone, setPhone] = useState("");
  if (!a) return null;
  const p = a.payload;
  return (
    <section className="split">
      <div className="plane plane-red stack">
        <p className="eyebrow">Your approval is needed</p>
        <h2 className="display" style={{ fontSize: "clamp(40px, 6cqi, 76px)" }}>Make this call?</h2>
        <ul style={{ margin: 0, paddingLeft: 20, fontWeight: 650 }}>{a.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        <div style={{ display: "grid", justifyItems: "start" }}>
          <button className="circle-btn circle-sm" disabled={busy} onClick={() => onDecide(a, { decision: "approve" })}>
            <span className="glyph" aria-hidden>→</span><span>Approve</span>
          </button>
        </div>
      </div>
      <div className="plane stack">
        <dl className="stack" style={{ margin: 0 }}>
          <div><dt className="label">Who</dt><dd style={{ margin: 0 }}>{p.contact_name || "—"} · {p.phone}</dd></div>
          <div><dt className="label">Language</dt><dd style={{ margin: 0 }}>{meta.languages[p.recipient_language] ?? "Unknown"}</dd></div>
          <div><dt className="label">Goal</dt><dd style={{ margin: 0 }}>{p.goal}</dd></div>
          <div><dt className="label">Will ask about</dt><dd style={{ margin: 0 }}><ul style={{ margin: 0, paddingLeft: 20 }}>{(p.ask_for as string[]).map((q) => <li key={q}>{q}</li>)}</ul></dd></div>
          {p.pending_commitment && <div><dt className="label">Agree to</dt><dd style={{ margin: 0, fontWeight: 750 }}>{p.pending_commitment}</dd></div>}
        </dl>
        <div className="row">
          <button className="btn" disabled={busy} onClick={() => setEditing(!editing)} aria-expanded={editing}>{editing ? "Close editor" : "Edit"}</button>
          <button className="btn" disabled={busy} onClick={() => onDecide(a, { decision: "reject" })}>Reject</button>
        </div>
        {editing && (
          <form className="stack" onSubmit={(e) => { e.preventDefault(); onDecide(a, { decision: "edit", instruction: instruction || undefined, contact_phone: phone || undefined }); }}>
            <label className="field"><span>Change the instructions</span><textarea className="input" value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="e.g. Also ask whether seat 12 is still free" /></label>
            <label className="field"><span>Or use a different number</span><PhoneInput value={phone} onChange={setPhone} /></label>
            <button className="btn btn-blue" disabled={busy || (!instruction && !phone)}>Re-plan with changes</button>
          </form>
        )}
      </div>
    </section>
  );
}

function SimConsole({ task, meta, busy, doit }: { task: Task; meta: Meta; busy: boolean; doit: (fn: () => Promise<Task>) => unknown }) {
  const [text, setText] = useState("");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const call = task.active_call!;
  const rl = task.recipient_language ?? "en-IN";
  const send = () => doit(async () => {
    const t = await api.simReply(task.id, call.id, text, key);
    setText(""); setKey(crypto.randomUUID());
    return t;
  });
  return (
    <section className="split on-blue">
      <form className="plane plane-blue stack" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <p className="eyebrow">Simulated call · you are the person being called</p>
        <h2 className="h3">Reply as {task.contact.name || "the recipient"}, in {meta.languages[rl] ?? rl}</h2>
        <label className="field"><span>What they say</span>
          <textarea className="input" style={{ color: "#000" }} value={text} onChange={(e) => setText(e.target.value)} required lang={rl.slice(0, 2)} /></label>
        <div className="row" style={{ alignItems: "start" }}>
          <button className="btn" disabled={busy || !text.trim()}>{busy ? "Agent is thinking…" : "Send reply"} <span className="arrow" aria-hidden>→</span></button>
          <VoiceButton language={rl} onText={(t) => setText(t)} enabled={meta.integrations.prisma_stt} label="Speak reply" />
        </div>
        {task.pending_turn && <p className="small">The last reply was saved but not processed yet (the model was unavailable). Send it again to retry.</p>}
      </form>
      <div className="plane stack">
        <p className="label">Simulate a network event</p>
        <p className="small">These stand in for real call-status events so you can test recovery.</p>
        <div className="row">
          {[["disconnected", "Drop the call"], ["no_answer", "No answer"], ["busy", "Busy"], ["rejected", "Declined"]].map(([o, l]) => (
            <button key={o} className="btn" disabled={busy} onClick={() => void doit(() => api.simOutcome(task.id, call.id, o))}>{l}</button>
          ))}
        </div>
      </div>
    </section>
  );
}

function FollowUpPanel({ task, busy, doit }: { task: Task; busy: boolean; doit: (fn: () => Promise<Task>) => unknown }) {
  return (
    <section className="split">
      <div className="plane plane-yellow stack">
        <p className="eyebrow">{task.follow_up_due ? "Follow-up time is here" : "Follow-up scheduled"}</p>
        <h2 className="h2">{fmtTime(task.follow_up_at) || "Now"}</h2>
        <p>{task.follow_up_reason}</p>
      </div>
      <div className="plane stack">
        <label className="check"><input type="checkbox" checked={task.follow_up_authorized} disabled={busy}
          onChange={(e) => void doit(() => api.authorizeFollowUp(task.id, e.target.checked))} />
          <span>Call them automatically at that time <span className="small">(your other approval settings still apply)</span></span></label>
        <div className="row">
          <button className="btn btn-blue" disabled={busy} onClick={() => void doit(() => api.followUp(task.id, "call"))}>Follow up now <span className="arrow" aria-hidden>→</span></button>
          <button className="btn" disabled={busy} onClick={() => void doit(() => api.followUp(task.id, "dismiss"))}>No follow-up needed</button>
        </div>
      </div>
    </section>
  );
}

function HelpPanel({ task, me, meta, busy, doit }: { task: Task; me: Me; meta: Meta; busy: boolean; doit: (fn: () => Promise<Task>) => unknown }) {
  const [note, setNote] = useState("");
  return (
    <section className="split">
      <form className="plane stack" onSubmit={(e) => { e.preventDefault(); void doit(() => api.inputs(task.id, { note })); }}>
        <h2 className="h3">{task.manual_takeover ? "You're handling this one." : "I'm stuck here and need you to decide."}</h2>
        <label className="field"><span>Add details or new instructions, and I'll plan again</span>
          <textarea className="input" value={note} onChange={(e) => setNote(e.target.value)} required /></label>
        <div className="row">
          <button className="btn btn-blue" disabled={busy || !note.trim()}>Hand back to the agent <span className="arrow" aria-hidden>→</span></button>
          <VoiceButton language={me.language!} onText={(t) => setNote((n) => (n ? n + " " : "") + t)} enabled={meta.integrations.prisma_stt} label="Say it" />
        </div>
      </form>
      <div className="plane plane-yellow stack">
        {task.contact.phone && <a className="btn" href={`tel:${task.contact.phone}`}>Call {task.contact.name || "them"} yourself</a>}
        <button className="btn" disabled={busy} onClick={() => void doit(() => api.resolve(task.id))}>Mark as resolved</button>
      </div>
    </section>
  );
}

function Transcript({ task, meta }: { task: Task; meta: Meta }) {
  if (!task.turns.length) return null;
  let lastCall = "";
  return (
    <section>
      <div className="plane" style={{ borderTop: "var(--seam)", borderBottom: "var(--rule)" }}><h2 className="h3">Conversation</h2></div>
      <div className="transcript">
        {task.turns.map((t) => {
          const sep = t.call_id !== lastCall;
          lastCall = t.call_id;
          const call = task.calls.find((c) => c.id === t.call_id);
          return (
            <div key={t.id} style={{ display: "contents" }}>
              {sep && call && <div className="call-sep">SIMULATED call · {fmtTime(call.created_at)} · {call.status.replace("_", " ")}</div>}
              <div className="turn" data-speaker={t.speaker}>
                <span className="who">{t.speaker === "agent" ? "Agent" : task.contact.name || "Recipient"}</span>
                <span lang={t.language?.slice(0, 2)}>{t.text}</span>
                {t.speaker === "agent" ? <ListenButton url={api.turnAudioUrl(task.id, t.id)} enabled={meta.integrations.timbre_tts} /> : <span />}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DeleteButton({ id }: { id: string }) {
  const [confirm, setConfirm] = useState(false);
  const { busy, error, run } = useAction();
  return (
    <>
      <button className="btn btn-ghost" disabled={busy} onClick={() => confirm
        ? void run(async () => { await api.deleteTask(id); location.hash = "#/"; })
        : setConfirm(true)}>
        {confirm ? "Confirm: permanently delete" : "Delete task data"}
      </button>
      {error && <span role="alert" className="small">{error}</span>}
    </>
  );
}
