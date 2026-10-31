import { useRef, useState } from "react";
import { ApiError, STATE_LABEL, TaskState, api } from "./api";
import { Recorder, startRecording } from "./web";

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, setError, run };
}

export function ErrorNotice({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="notice notice-error" role="alert">{error}</div>;
}

const TONE: Partial<Record<TaskState, string>> = {
  AWAITING_INPUT: "act", AWAITING_APPROVAL: "act", NEEDS_HUMAN_HELP: "act", WAITING_RETRY: "act",
  PLANNING: "live", CALLING: "live", CLARIFYING: "live", VERIFYING: "live",
  COMPLETED: "done", WAITING_FOLLOW_UP: "done", FAILED: "off", CANCELLED: "off",
};
const GLYPH: Record<string, string> = { act: "▲", live: "●", done: "■", off: "—" };

export function StateTag({ state }: { state: TaskState }) {
  const tone = TONE[state] ?? "off";
  return <span className="state" data-tone={tone}><span aria-hidden>{GLYPH[tone]}</span>{STATE_LABEL[state]}</span>;
}

/** Records up to 30 s, transcribes with Prisma (server-side), hands back text for the user to confirm. */
export function VoiceButton({ language, onText, enabled, big = false, label = "Speak" }: {
  language: string; onText: (t: string) => void; enabled: boolean; big?: boolean; label?: string;
}) {
  const [phase, setPhase] = useState<"idle" | "rec" | "stt">("idle");
  const [msg, setMsg] = useState<string | null>(null);
  const rec = useRef<Recorder | null>(null);

  async function stop() {
    const r = rec.current;
    rec.current = null;
    if (!r) return;
    setPhase("stt");
    try {
      const wav = await r.stop();
      const { transcript, empty } = await api.stt(wav, language);
      if (empty) setMsg("Didn't catch that — try again a little closer to the mic, or type.");
      else onText(transcript);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Transcription failed");
    } finally {
      setPhase("idle");
    }
  }

  async function toggle() {
    setMsg(null);
    if (phase === "rec") return stop();
    try {
      rec.current = await startRecording(() => void stop());
      setPhase("rec");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Microphone unavailable");
    }
  }

  const text = phase === "rec" ? "Stop" : phase === "stt" ? "Listening…" : label;
  return (
    <div className="stack" style={{ display: "grid", justifyItems: big ? "center" : "start" }}>
      <button type="button" className={big ? "circle-btn" : "circle-btn circle-sm"} aria-pressed={phase === "rec"}
        disabled={!enabled || phase === "stt"} onClick={toggle}
        aria-label={phase === "rec" ? "Stop recording" : label}>
        <span className="glyph" aria-hidden>{phase === "rec" ? "■" : "●"}</span>
        <span>{text}</span>
      </button>
      {!enabled && <p className="small">Voice input needs the Gnani key on the server. You can type instead.</p>}
      {msg && <p className="small" role="status">{msg}</p>}
    </div>
  );
}

export function ListenButton({ url, enabled }: { url: string; enabled: boolean }) {
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  if (!enabled) return null;
  return (
    <button type="button" className="btn btn-ghost" disabled={state === "loading"} onClick={() => {
      setState("loading");
      const a = new Audio(url);
      a.oncanplay = () => setState("idle");
      a.onerror = () => setState("error");
      void a.play().catch(() => setState("error"));
    }}>
      <span aria-hidden>▶</span> {state === "loading" ? "Loading…" : state === "error" ? "Audio unavailable" : "Listen"}
    </button>
  );
}

export function LanguageGrid({ languages, value, onChange }: {
  languages: Record<string, string>; value: string | null; onChange: (code: string) => void;
}) {
  return (
    <div className="lang-grid" role="group" aria-label="Language">
      {Object.entries(languages).map(([code, name]) => (
        <button key={code} type="button" aria-pressed={value === code} onClick={() => onChange(code)}>{name}</button>
      ))}
    </div>
  );
}

export function fmtTime(iso: string | null) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** 10-digit Indian phone number: digits only, never more than 10 (the backend enforces the same rule). */
export function PhoneInput({ value, onChange, required = false }: {
  value: string; onChange: (digits: string) => void; required?: boolean;
}) {
  return (
    <input className="input" type="tel" inputMode="numeric" autoComplete="tel-national" maxLength={10}
      pattern="\d{10}" title="Exactly 10 digits" placeholder="10-digit number" required={required}
      value={value} onChange={(e) => onChange(e.target.value.replace(/\D/g, "").slice(0, 10))} />
  );
}
