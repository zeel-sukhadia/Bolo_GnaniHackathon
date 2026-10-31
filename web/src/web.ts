// Web-only platform code: microphone → WAV, service worker, Web Push.
// Prisma accepts WAV/MP3/OGG/FLAC/AAC/M4A (not WebM), so we encode 16 kHz mono WAV ourselves.

const MAX_SECONDS = 30; // Prisma v2.5 REST limit

export interface Recorder { stop: () => Promise<Blob>; cancel: () => void }

export async function startRecording(onAutoStop: () => void): Promise<Recorder> {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone needs HTTPS and a supported browser.");
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  source.connect(proc);
  proc.connect(ctx.destination);
  const timer = setTimeout(onAutoStop, (MAX_SECONDS - 1) * 1000);
  const close = () => {
    clearTimeout(timer);
    proc.disconnect(); source.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    void ctx.close();
  };
  return {
    cancel: close,
    stop: async () => {
      close();
      return encodeWav(chunks, ctx.sampleRate, 16000);
    },
  };
}

function encodeWav(chunks: Float32Array[], inRate: number, outRate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const ratio = inRate / outRate;
  const outLen = Math.floor(total / ratio);
  const flat = new Float32Array(total);
  let o = 0;
  for (const c of chunks) { flat.set(c, o); o += c.length; }
  const buf = new DataView(new ArrayBuffer(44 + outLen * 2));
  const str = (off: number, s: string) => [...s].forEach((ch, i) => buf.setUint8(off + i, ch.charCodeAt(0)));
  str(0, "RIFF"); buf.setUint32(4, 36 + outLen * 2, true); str(8, "WAVE"); str(12, "fmt ");
  buf.setUint32(16, 16, true); buf.setUint16(20, 1, true); buf.setUint16(22, 1, true);
  buf.setUint32(24, outRate, true); buf.setUint32(28, outRate * 2, true); buf.setUint16(32, 2, true);
  buf.setUint16(34, 16, true); str(36, "data"); buf.setUint32(40, outLen * 2, true);
  for (let i = 0; i < outLen; i++) {
    // ponytail: nearest-sample decimation; fine for speech at 16 kHz, use a low-pass if STT quality suffers
    const s = Math.max(-1, Math.min(1, flat[Math.floor(i * ratio)]));
    buf.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

export function registerServiceWorker() {
  if ("serviceWorker" in navigator && import.meta.env.PROD) {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js"));
  }
}

export function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

export async function enablePush(vapidPublicKey: string): Promise<PushSubscriptionJSON> {
  const reg = await navigator.serviceWorker.ready;
  if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications were not allowed.");
  const key = Uint8Array.from(atob(vapidPublicKey.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(vapidPublicKey.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
  const sub = (await reg.pushManager.getSubscription()) ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }));
  return sub.toJSON();
}
