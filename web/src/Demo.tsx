// One-screen demo for screen recording (#/demo). The agent side is the real engine (plan, approval,
// call, follow-ups, verification, summary). The person being called is either a real person (you, replying
// by voice or text) or, if switched to "AI", played by Claude through /demo/contact-reply and spoken with a
// second Timbre voice. The call itself is simulated and labelled so.
import { useEffect, useRef, useState } from "react";
import { FACT_LABEL, Me, Meta, Task, api } from "./api";
import { StateTag, VoiceButton, PhoneInput } from "./ui";

const REQUESTER_LANG = "gu-IN";
const RECIPIENT_LANG = "hi-IN"; // default for the person being called; selectable on screen
const DOING: Record<string, string> = {
  open: "Introducing itself as an AI assistant and checking it's a good time to talk",
  resume: "Calling back and picking up where it left off",
  ask: "Asking for the information you need",
  clarify: "That answer was vague, so it's asking a targeted follow-up",
  verify: "Reading the answer back to confirm it",
  verify_fix: "Something in the read-back was off, so it's asking what to correct",
  close: "Wrapping up the call politely",
};
const STEPS: [string, string[]][] = [
  ["Understand your request", ["NEW", "PLANNING", "AWAITING_INPUT"]],
  ["Ask for your approval", ["AWAITING_APPROVAL"]],
  ["Call them in their language", ["CALLING", "CLARIFYING"]],
  ["Confirm the answer", ["VERIFYING"]],
  ["Report back to you", ["COMPLETED", "WAITING_FOLLOW_UP", "NEEDS_HUMAN_HELP", "FAILED", "CANCELLED"]],
];
const NEEDS_YOU = ["AWAITING_INPUT", "NEEDS_HUMAN_HELP"];
type Phase = "idle" | "dialing" | "ringing" | "answered" | "live" | "ended";

let current: HTMLAudioElement | null = null;
function play(src: string): Promise<void> {
  return new Promise((done) => {
    const a = (current = new Audio(src));
    a.onended = () => done();
    a.onerror = () => done();
    a.play().catch(() => done());
  });
}
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Indian-style ringback tone (two bursts, then a pause) until the returned stop() is called. */
function ringback(): () => void {
  const ctx = new AudioContext();
  const gain = ctx.createGain();
  gain.gain.value = 0;
  gain.connect(ctx.destination);
  for (const f of [400, 450]) {
    const o = ctx.createOscillator();
    o.frequency.value = f;
    o.connect(gain);
    o.start();
  }
  for (let i = 0; i < 20; i++) {
    const s = ctx.currentTime + 0.1 + i * 3;
    for (const [on, off] of [[0, 0.4], [0.6, 1.0]]) {
      gain.gain.setValueAtTime(0.06, s + on);
      gain.gain.setValueAtTime(0, s + off);
    }
  }
  return () => void ctx.close();
}

const initials = (n: string) => n.split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase() || "?";
const mmss = (ms: number) => `${String(Math.floor(ms / 60000)).padStart(2, "0")}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;

export default function Demo({ meta }: { me: Me; meta: Meta }) {
  const [youLang, setYouLang] = useState(REQUESTER_LANG);
  const [themLang, setThemLang] = useState(RECIPIENT_LANG);
  const [request, setRequest] = useState("");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [shown, setShown] = useState(0);            // turns revealed so far (in step with the audio)
  const [pending, setPending] = useState<string | null>(null); // contact line spoken, agent still thinking
  const [speaking, setSpeaking] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [startedAt, setStartedAt] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [reported, setReported] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [end, setEnd] = useState(false);
  const [en, setEn] = useState<Record<string, string>>({});
  // Who answers the call: true = a real person (you) replies by voice/text, false = AI plays the contact.
  const [human, setHuman] = useState(true);
  const [waiting, setWaiting] = useState(false); // true while the call waits for the human's reply
  const [draft, setDraft] = useState("");
  const replyResolver = useRef<((text: string) => void) | null>(null);
  const asked = useRef(new Set<string>());
  const run = useRef(0);
  const played = useRef(0); // turns already shown/played, so a second call doesn't replay the first
  const stopRing = useRef<(() => void) | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const lang = (c: string) => meta.languages[c] ?? c;

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: "smooth" });
  }, [shown, pending, phase, en, waiting]);

  useEffect(() => {
    if (phase !== "live") return;
    const t = setInterval(() => setElapsed(Date.now() - startedAt), 1000);
    return () => clearInterval(t);
  }, [phase, startedAt]);

  function translate(...texts: (string | null | undefined)[]) {
    const todo = texts.filter((t): t is string => !!t && !asked.current.has(t));
    if (!todo.length) return;
    todo.forEach((t) => asked.current.add(t));
    api.demoTranslate(todo)
      .then(({ english }) => setEn((m) => ({ ...m, ...Object.fromEntries(todo.map((t, i) => [t, english[i]])) })))
      .catch(() => todo.forEach((t) => asked.current.delete(t)));
  }

  function hangUpRing() {
    stopRing.current?.();
    stopRing.current = null;
  }

  /** Pauses the call until the human sends a reply (spoken via Prisma STT or typed). */
  function awaitHuman(): Promise<string> {
    return new Promise((resolve) => {
      setDraft("");
      setWaiting(true);
      replyResolver.current = (text: string) => {
        replyResolver.current = null;
        setWaiting(false);
        setDraft("");
        resolve(text);
      };
    });
  }

  function reset() {
    run.current++;
    current?.pause();
    hangUpRing();
    replyResolver.current = null;
    played.current = 0;
    setTask(null); setShown(0); setPending(null); setSpeaking(null); setPhase("idle"); setElapsed(0);
    setReported(false); setBusy(false); setError(null); setEnd(false);
    setWaiting(false); setDraft("");
  }

  async function guarded(fn: (live: () => boolean) => Promise<void>) {
    const token = run.current;
    const live = () => run.current === token;
    setBusy(true); setError(null);
    try {
      await fn(live);
    } catch (e) {
      if (live()) { hangUpRing(); setPhase("idle"); setError(e instanceof Error ? e.message : "Something went wrong"); }
    } finally {
      if (live()) setBusy(false);
    }
  }

  const start = () => guarded(async (live) => {
    translate(request);
    let t = await api.createTask({ request, contact_name: name || undefined, contact_phone: phone || undefined,
      recipient_language: themLang, requester_language: youLang });
    setTask(t);
    await proceed(t, live);
  });

  /** Wait out planning, then either ask the user (the panel renders from state), wait for approval, or call. */
  async function proceed(t: Task, live: () => boolean) {
    while (t.state === "PLANNING") {
      await wait(1500);
      if (!live()) return;
      t = await api.task(t.id);
    }
    if (!live()) return;
    setTask(t);
    translate(t.goal_for_requester, t.status_detail, ...t.missing_inputs.filter((q) => !SPECIAL.includes(q)));
    if (t.active_call) await connect(t, live);
  }

  // Only reached when the engine itself paused for the user (AWAITING_INPUT / NEEDS_HUMAN_HELP).
  const answer = (body: Parameters<typeof api.inputs>[1]) => guarded(async (live) => {
    setReported(false);
    const t = await api.inputs(task!.id, body);
    setTask(t);
    await proceed(t, live);
  });

  const decide = (decision: "approve" | "reject") => guarded(async (live) => {
    const a = task!.approvals.find((x) => x.status === "pending")!;
    if (decision === "reject") {
      setTask(await api.decide(task!.id, a.id, { decision }));
      return;
    }
    setPhase("dialing");
    const placed = api.decide(task!.id, a.id, { decision }); // the opening line is composed while it rings
    placed.catch(() => {}); // handled when awaited below
    await wait(1200);
    if (!live()) return;
    setPhase("ringing");
    stopRing.current = ringback();
    const ringFrom = Date.now();
    const t = await placed;
    await wait(Math.max(0, 4000 - (Date.now() - ringFrom)));
    if (!live()) return;
    setTask(t);
    if (t.active_call) await connect(t, live);
  });

  async function connect(t: Task, live: () => boolean) {
    hangUpRing();
    setPhase("answered");
    await wait(1100);
    if (!live()) return;
    setStartedAt(Date.now());
    setElapsed(0);
    setPhase("live");
    await wait(400);
    await converse(t, live);
  }

  async function converse(t: Task, live: () => boolean) {
    for (;;) {
      for (; played.current < t.turns.length; played.current++) {
        const turn = t.turns[played.current];
        setShown(played.current + 1);
        if (turn.speaker === "agent") { // contact lines were already shown before the agent processed them
          translate(turn.text);
          setSpeaking(turn.id);
          await play(api.turnAudioUrl(t.id, turn.id));
          setSpeaking(null);
          await wait(350);
        }
        if (!live()) return;
      }
      if (!t.active_call) break;

      // The person being called replies: either you (human) or the AI stand-in.
      let text: string;
      let audioB64: string | undefined;
      if (human) {
        text = await awaitHuman(); // waits until you speak or type and press "Send reply"
        if (!live()) return;
        translate(text);           // English subtitle for the reply
      } else {
        const line = await api.demoContactReply(t.id);
        if (!live()) return;
        text = line.text;
        audioB64 = line.audio_mp3_b64 || undefined;
        asked.current.add(line.text);
        setEn((m) => ({ ...m, [line.text]: line.english }));
      }

      setPending(text);
      const reply = api.simReply(t.id, t.active_call.id, text, crypto.randomUUID());
      reply.catch(() => {}); // handled when awaited below
      if (audioB64) {
        setSpeaking("pending");
        await play(`data:audio/mpeg;base64,${audioB64}`);
        setSpeaking(null);
      }
      t = await reply;
      if (!live()) return;
      setPending(null);
      setTask(t);
    }
    setPhase("ended");
    if (!t.result_summary) return;
    translate(t.result_summary);
    await wait(900);
    setReported(true);
    setSpeaking("summary");
    await play(api.summaryAudioUrl(t.id));
    setSpeaking(null);
    await wait(1500);
    if (live() && !NEEDS_YOU.includes(t.state)) setEnd(true);
  }

  const turns = task ? task.turns.slice(0, shown) : [];
  const lastAgent = [...turns].reverse().find((x) => x.speaker === "agent");
  const step = task ? STEPS.findIndex(([, s]) => s.includes(task.state)) : -1;
  const finished = reported && !speaking;
  const dialing = phase === "dialing" || phase === "ringing" || phase === "answered";
  const approval = task?.state === "AWAITING_APPROVAL" && !dialing ? task.approvals.find((x) => x.status === "pending") : undefined;
  const who = task?.contact.name || name || "Contact";
  const badPhone = phone.length > 0 && phone.length !== 10;
  const needsYou = !!task && NEEDS_YOU.includes(task.state) && !task.active_call && !busy && !speaking;
  const callLang = task?.recipient_language || themLang;
  const doing = waiting ? `Waiting for ${who} to reply`
    : pending ? "Listening to the reply and checking whether it really answers the question"
    : dialing ? "Placing the call" : phase === "live" && lastAgent?.intent ? DOING[lastAgent.intent] : null;
  const callStatus = phase === "ended" ? `Call ended · ${mmss(elapsed)}`
    : waiting ? `Waiting for ${human ? "your" : who + "'s"} reply · ${mmss(elapsed)}`
    : speaking === "pending" ? `${who} is speaking`
    : speaking && speaking !== "summary" ? "Bolo is speaking"
    : pending ? "Bolo is thinking…" : `Connected · ${mmss(elapsed)}`;
  const En = ({ text }: { text: string | null | undefined }) => (text && en[text] ? <div className="en">{en[text]}</div> : null);

  return (
    <div className="demo">
      <header className="demo-top">
        <a className="brand" href="#/" aria-label="Back to Bolo">
          <span className="brand-mark" aria-hidden><i className="c" /><i className="t" /><i className="s" /></span>
          Bolo
        </a>
        <span className="state" data-tone="act">▲ Simulated call: no real phone call is placed</span>
        <button className="btn" style={{ marginLeft: "auto", minHeight: 40 }} onClick={reset}>Restart</button>
      </header>

      <div className="demo-cols">
        <section className="demo-col demo-you" aria-label="You">
          <h2>You <span className="state">{lang(youLang)}</span></h2>
          {!task ? (
            <>
              <fieldset className="demo-step">
                <legend><b>1</b> Your language</legend>
                <label className="field"><span className="small">Bolo will listen to you and report back in this language.</span>
                  <select className="input" value={youLang} onChange={(e) => setYouLang(e.target.value)}>
                    {Object.entries(meta.languages).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                  </select></label>
              </fieldset>
              <fieldset className="demo-step">
                <legend><b>2</b> What should Bolo find out?</legend>
                <div className="row" style={{ alignItems: "center" }}>
                  <VoiceButton language={youLang} onText={(t) => setRequest((r) => (r ? r + " " : "") + t)}
                    enabled={meta.integrations.prisma_stt} label="Tap and speak" />
                  <span className="small">Tap the button and say what you need, or type it below.</span>
                </div>
                <label className="field"><span className="sr-only">Your request</span>
                  <textarea className="input" rows={3} value={request} onChange={(e) => setRequest(e.target.value)}
                    lang={youLang.slice(0, 2)} placeholder="e.g. Ask the shop if my order has been sent, and when it will arrive" /></label>
              </fieldset>
              <fieldset className="demo-step">
                <legend><b>3</b> Who should Bolo call?</legend>
                <div className="grid2">
                  <label className="field"><span>Their name</span>
                    <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Ravi" /></label>
                  <label className="field"><span>Their phone number</span>
                    <PhoneInput value={phone} onChange={setPhone} /></label>
                </div>
                <label className="field"><span>Language they speak</span>
                  <select className="input" value={themLang} onChange={(e) => setThemLang(e.target.value)}>
                    {Object.entries(meta.languages).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                  </select>
                  <span className="small">Bolo will talk to them only in this language.</span></label>
                <label className="field"><span>Who answers the call?</span>
                  <select className="input" value={human ? "human" : "ai"} onChange={(e) => setHuman(e.target.value === "human")}>
                    <option value="human">Me (I reply by voice or text)</option>
                    <option value="ai">AI (automatic)</option>
                  </select>
                  <span className="small">The call is simulated, but the replies can come from a real person.</span></label>
              </fieldset>
              <button className="btn btn-blue" disabled={busy || request.trim().length < 5 || badPhone} onClick={() => void start()}>
                Send to Bolo <span className="arrow" aria-hidden>→</span></button>
              {request.trim().length < 5 && <p className="small" style={{ margin: 0 }}>Tell Bolo what you need to continue.</p>}
              {badPhone && <p className="small" style={{ margin: 0 }}>The phone number must be exactly 10 digits.</p>}
            </>
          ) : (
            <>
              <div className="bubble" data-side="contact">
                <div className="who">You said</div>
                <div className="said" lang={youLang.slice(0, 2)}>{task.original_request}</div>
                <En text={task.original_request} />
              </div>
              {!reported && (dialing || phase === "live") && (
                <p className="small">{human
                  ? "Bolo is on the call. Reply as the person being called, in the call panel on the right."
                  : "Bolo is on the call. You don't need to do anything."}</p>
              )}
              {needsYou && <DetailsPanel key={task.updated_at} task={task} meta={meta} youLang={youLang} busy={busy}
                en={en} onSubmit={(b) => void answer(b)} />}
              {reported && task.result_summary && (
                <div className="bubble" data-side="agent" data-speaking={speaking === "summary"}>
                  <div className="who">Bolo's result for you, in {lang(task.requester_language)}</div>
                  <div className="said" lang={task.requester_language.slice(0, 2)}>{task.result_summary}</div>
                  <En text={task.result_summary} />
                </div>
              )}
            </>
          )}
        </section>

        <section className="demo-col" aria-label="Agent">
          <h2>Bolo agent</h2>
          <ol className="demo-steps">
            {STEPS.map(([label], i) => (
              <li key={label} data-at={step < 0 || i > step ? "todo" : i < step || finished ? "done" : "now"}>{label}</li>
            ))}
          </ol>
          {task?.state === "PLANNING" && <p className="loading">Working out the goal and what to ask…</p>}
          {task?.goal && <div className="demo-card"><div className="label">Goal</div>{task.goal}</div>}
          {approval && (
            <div className="demo-card" style={{ background: "var(--dp-color-accent-secondary)" }}>
              <div className="label">Your approval is needed</div>
              <p style={{ margin: "6px 0", fontWeight: 700 }}>Call {approval.payload.contact_name || approval.payload.phone} in {lang(approval.payload.recipient_language)}?</p>
              <ul className="small" style={{ margin: "0 0 10px", paddingLeft: 18 }}>{approval.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              <div className="row">
                <button className="btn btn-blue" disabled={busy} onClick={() => void decide("approve")}>Approve call</button>
                <button className="btn" disabled={busy} onClick={() => void decide("reject")}>Reject</button>
              </div>
            </div>
          )}
          {doing && <div className="demo-card" style={{ background: "var(--dp-color-accent-warm)" }}><div className="label">Right now</div>{doing}</div>}
          {task && task.required_info.length > 0 && (
            <div className="stack">
              <div className="label">What it needs to find out</div>
              {task.required_info.map((f) => (
                <div key={f.key} className="fact" data-status={f.status}>
                  <div className="status">{FACT_LABEL[f.status] ?? f.status}</div>
                  <div className="small" style={{ marginTop: 6 }}>{f.description}</div>
                  {f.value && <div className="value">{f.value}</div>}
                </div>
              ))}
            </div>
          )}
          {needsYou && <div className="demo-card" style={{ background: "var(--dp-color-accent-secondary)" }}>
            <div className="label">Paused: waiting for you</div>Bolo needs a detail from you before it can go on. Answer on the left.</div>}
          {task?.state === "FAILED" && !task.active_call && (
            <div className="demo-card"><StateTag state={task.state} /> <p className="small">{task.status_detail}</p></div>
          )}
          {error && <div className="notice notice-error" role="alert" style={{ margin: 0 }}>{error}</div>}
        </section>

        <section className="demo-col demo-call" aria-label="Call">
          {phase === "idle" || dialing ? (
            <div className="dial" data-phase={phase}>
              <div className="avatar" aria-hidden>{initials(who)}</div>
              <div className="dial-name">{who}</div>
              <div className="small">{task?.contact.phone || phone} · {lang(callLang)}</div>
              <div className="dial-status" role="status">
                {phase === "dialing" ? "Calling…" : phase === "ringing" ? "Ringing…" : phase === "answered" ? "● Connected"
                  : task?.state === "AWAITING_APPROVAL" ? "Waiting for your approval" : "No call yet"}
              </div>
              {dialing && (
                <div className="hangup" aria-hidden>
                  {/* standard handset (Material "call"); CSS turns it 135° into the end-call position while ringing */}
                  <svg viewBox="0 0 24 24" width="32" height="32" fill="#fff"><path d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z" /></svg>
                </div>
              )}
              <div className="small dial-note">Simulated call · Bolo AI calls in {lang(callLang)}</div>
            </div>
          ) : (
            <>
              <div className="callbar">
                <div className="avatar" aria-hidden>{initials(who)}</div>
                <div>
                  <div style={{ fontWeight: 800 }}>{who}</div>
                  <div className="small" role="status">{callStatus}</div>
                </div>
                <span className="state" data-tone={phase === "live" ? "live" : "off"} style={{ marginLeft: "auto" }}>
                  {phase === "live" ? "● Simulated call" : "Call ended"}</span>
              </div>
              <div className="thread" ref={threadRef}>
                <p className="call-sep">
                  {human
                    ? `You are playing ${who} in this demo, and the call itself is simulated. Both sides speak only ${lang(callLang)}; English lines are subtitles.`
                    : `The person answering is played by AI for this demo. Both sides speak only ${lang(callLang)}; English lines are subtitles.`}
                </p>
                {turns.map((t) => (
                  <div key={t.id} className="bubble" data-side={t.speaker === "agent" ? "agent" : "contact"} data-speaking={speaking === t.id}>
                    <div className="who">{t.speaker === "agent" ? "Bolo (AI assistant)" : who}{t.intent === "clarify" ? " · follow-up" : ""}</div>
                    <div className="said" lang={callLang.slice(0, 2)}>{t.text}</div>
                    <En text={t.text} />
                  </div>
                ))}
                {pending && (
                  <div className="bubble" data-side="contact" data-speaking={speaking === "pending"}>
                    <div className="who">{who}</div>
                    <div className="said" lang={callLang.slice(0, 2)}>{pending}</div>
                    <En text={pending} />
                  </div>
                )}
                {phase === "ended" && <p className="small" style={{ alignSelf: "center" }}>— Call ended —</p>}
              </div>
              {human && phase === "live" && waiting && (
                <form className="demo-card stack" onSubmit={(e) => {
                  e.preventDefault();
                  const text = draft.trim();
                  if (text) replyResolver.current?.(text);
                }}>
                  <div className="label">Your turn: reply as {who} in {lang(callLang)}</div>
                  <VoiceButton language={callLang} onText={(t) => setDraft((d) => (d ? d + " " : "") + t)}
                    enabled={meta.integrations.prisma_stt} label="Tap and speak" />
                  <textarea className="input" rows={2} value={draft} lang={callLang.slice(0, 2)}
                    placeholder="Speak, or type your reply here" onChange={(e) => setDraft(e.target.value)} />
                  <button className="btn btn-blue" disabled={!draft.trim()}>Send reply <span className="arrow" aria-hidden>→</span></button>
                </form>
              )}
            </>
          )}
        </section>
      </div>

      {end && (
        <div className="demo-end" role="dialog" aria-label="Demo finished">
          <div className="demo-card stack" style={{ maxWidth: 720, padding: 32 }}>
            <p className="eyebrow">What's next</p>
            <h2 className="h2">In the real product, Bolo places this call over a real phone line.</h2>
            <p>You spoke {lang(youLang)}. The other person heard {lang(callLang)}. Bolo asked a follow-up when the answer was vague, confirmed it, and reported back to you in your language.</p>
            <div className="row">
              <button className="btn btn-blue" onClick={reset}>Run again</button>
              <button className="btn" onClick={() => setEnd(false)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const SPECIAL = ["contact_phone", "contact_choice", "recipient_language"];

/** Asks for exactly what the engine said it's missing (task.missing_inputs), or for new instructions if it's stuck. */
function DetailsPanel({ task, meta, youLang, busy, en, onSubmit }: {
  task: Task; meta: Meta; youLang: string; busy: boolean; en: Record<string, string>;
  onSubmit: (b: Parameters<typeof api.inputs>[1]) => void;
}) {
  const [note, setNote] = useState("");
  const [phone, setPhone] = useState("");
  const [rlang, setRlang] = useState("");
  const needPhone = task.missing_inputs.some((q) => q === "contact_phone" || q === "contact_choice");
  const needLang = task.missing_inputs.includes("recipient_language");
  const questions = task.missing_inputs.filter((q) => !SPECIAL.includes(q));
  const stuck = task.state === "NEEDS_HUMAN_HELP";
  const needNote = stuck || questions.length > 0;
  const tongue = youLang.slice(0, 2);
  return (
    <form className="demo-card stack" style={{ background: "var(--dp-color-accent-secondary)" }} onSubmit={(e) => {
      e.preventDefault();
      onSubmit({ note: note.trim() || undefined, contact_phone: phone || undefined, recipient_language: rlang || undefined });
    }}>
      <div className="label">{stuck ? "Bolo needs your help to continue" : "Bolo needs a little more from you"}</div>
      {stuck && task.status_detail && <p className="small" style={{ margin: 0 }}>{task.status_detail}{en[task.status_detail] && <span className="en"> {en[task.status_detail]}</span>}</p>}
      {questions.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: 20, fontWeight: 650 }}>
          {questions.map((q) => <li key={q} lang={tongue}>{q}{en[q] && <div className="en">{en[q]}</div>}</li>)}
        </ul>
      )}
      {needPhone && (
        <label className="field"><span>Their phone number</span>
          <PhoneInput value={phone} onChange={setPhone} required /></label>
      )}
      {needLang && (
        <label className="field"><span>Language they speak</span>
          <select className="input" required value={rlang} onChange={(e) => setRlang(e.target.value)}>
            <option value="">Choose…</option>
            {Object.entries(meta.languages).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
          </select></label>
      )}
      {needNote && (
        <>
          <label className="field"><span>{stuck ? "Add details or new instructions" : "Your answer"}</span>
            <textarea className="input" rows={2} required value={note} onChange={(e) => setNote(e.target.value)} lang={tongue} /></label>
          <VoiceButton language={youLang} onText={(t) => setNote((n) => (n ? n + " " : "") + t)}
            enabled={meta.integrations.prisma_stt} label="Answer by voice" />
        </>
      )}
      <button className="btn btn-blue" disabled={busy}>Continue <span className="arrow" aria-hidden>→</span></button>
    </form>
  );
}