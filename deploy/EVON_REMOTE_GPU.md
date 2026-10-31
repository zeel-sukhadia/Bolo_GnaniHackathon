# Run Evon v3.3 on a remote GPU and connect your local backend

```
laptop: React PWA ──► FastAPI (localhost:8000) ──HTTPS + Bearer key──► tunnel ──► vLLM on GPU (127.0.0.1:8000)
```

The backend is the only thing that talks to Evon. The browser never sees the URL or the key.

## 0. What you need to know first (checked against Hugging Face, Oct 2026)

| Fact | Value |
|---|---|
| Repo | `gnani/gnani-evon-v3.3-30B-A3B`. The model card's snippets say `gnani/gnani-evon-v3.3`, which redirects here. |
| Access | Public, not gated, Apache-2.0. No Hugging Face token is required (one helps with download rate limits). |
| Architecture | `nemotron_h`: a Mamba2/Transformer hybrid MoE, 30B total and about 3.5B active per token. It needs `--trust-remote-code`. |
| Weights | BF16, **63.5 GB** to download, so you need about 64 GB of VRAM before any cache. |
| Runtimes (model card) | vLLM ≥ 0.12.0, SGLang, Transformers ≥ 5.3.0. Hardware: H100 80GB, H200, A100. |
| Hosted API | **None.** Downloading the weights does not give you an endpoint. You run the server yourself. |

### Which GPU can run it

| GPU (Colab name) | VRAM | Official BF16 with vLLM | Notes |
|---|---|---|---|
| **T4 (free tier)** | 15 GB | **No** | **Option B** works: a community 4-bit GGUF (17.1 GiB) with the overflow in system RAM. It's slower and lower quality. |
| L4 | 22.5 GB | **No** | Option B; the 17.1 GiB GGUF fits entirely on the GPU, so it's faster than on a T4. |
| A100 40GB | 40 GB | **No** | Option B with a larger quant (Q5_K_M / Q8_0). |
| **A100 80GB / H100 80GB** | 80 GB | **Yes** | Recommended. Use `--max-model-len 32768` to leave room. |

Colab's GPU types and availability change, and paid runtimes cost compute units. Colab sessions also disconnect when idle and have a maximum lifetime, which changes your tunnel URL. For a stable demo, renting an 80 GB GPU by the hour (RunPod, Lambda, Vast.ai, GCP/AWS) with the same commands below is more reliable. Everything in step 1 works the same there, minus the `google.colab` bits.

## 1. Option A (recommended): official BF16 weights, vLLM, 80 GB GPU

On your laptop, generate the shared key once, then put it in your local `.env` **and** in Colab's Secrets panel (🔑 icon) as `EVON_API_KEY`:

```powershell
backend\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
```

In a Colab notebook (Runtime → Change runtime type → **H100** or **A100** with 80 GB), run these cells in order.

**Cell 1: check the GPU and disk.** Stop here if the total VRAM is under ~80 GB or free disk is under ~80 GB.
```python
!nvidia-smi --query-gpu=name,memory.total --format=csv
!df -h / | tail -1
```

**Cell 2: install vLLM.** If Colab asks you to restart the runtime afterwards, do that, then continue from Cell 3.
```python
!pip install -q -U "vllm>=0.12.0"
```

**Cell 3: start the server.** It binds to 127.0.0.1 only and requires the key. It is never exposed without auth.
```python
import subprocess
from google.colab import userdata
key = userdata.get("EVON_API_KEY")
log = open("/content/vllm.log", "w")
vllm = subprocess.Popen([
    "vllm", "serve", "gnani/gnani-evon-v3.3-30B-A3B",
    "--served-model-name", "model",
    "--trust-remote-code",
    "--max-model-len", "32768",
    "--gpu-memory-utilization", "0.92",
    "--reasoning-parser", "nano_v3",
    "--host", "127.0.0.1", "--port", "8000",
    "--api-key", key,
], stdout=log, stderr=subprocess.STDOUT)
```

**Cell 4: wait until it's ready.** The first run downloads 63.5 GB, so expect 10 to 30 minutes.
```python
import time, urllib.request
while True:
    if vllm.poll() is not None:
        print(open("/content/vllm.log").read()[-4000:]); raise SystemExit("vLLM exited, see log above")
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/v1/models", headers={"Authorization": f"Bearer {key}"})
        print(urllib.request.urlopen(req, timeout=5).read().decode()); break
    except Exception:
        print(open("/content/vllm.log").read().splitlines()[-1:] or "starting…"); time.sleep(30)
```

**Cell 5: open an HTTPS tunnel.** This uses a Cloudflare quick tunnel, which needs no account. Requests still need the bearer key.
```python
!wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /content/cloudflared && chmod +x /content/cloudflared
import re, subprocess, time
tlog = open("/content/tunnel.log", "w")
tunnel = subprocess.Popen(["/content/cloudflared", "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8000"],
                          stdout=tlog, stderr=subprocess.STDOUT)
for _ in range(30):
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open("/content/tunnel.log").read())
    if m: break
    time.sleep(2)
print("EVON_BASE_URL=" + m.group(0) + "/v1")
```

**Cell 6: test through the public URL, exactly as your backend will.**
```python
import json, urllib.request
url = m.group(0) + "/v1/chat/completions"
body = {"model": "model", "max_tokens": 200, "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": "In Tamil, ask how many minutes until the bus arrives."}]}
req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
print(json.load(urllib.request.urlopen(req, timeout=90))["choices"][0]["message"]["content"])
```

Keep the notebook open. If Cell 5 is re-run or the runtime restarts, **the URL changes**. Re-run Cell 5 and update `.env`.

A quick tunnel cuts any request after 100 s. Keep `EVON_TIMEOUT` below that (the default is 90). For a URL that doesn't change, use a named Cloudflare tunnel or ngrok with a reserved domain (both need an account).

## 2. Option B: Tesla T4 (15 GB) with a community GGUF and llama.cpp

**Read this first.** `luminenio/gnani-evon-v3.3-30B-A3B-GGUF` is a **community** quantization, not a Gnani release. Its licence is the NVIDIA Nemotron Open Model License with Apache-2.0 modifications; read `LICENSE`, `LICENSE-NVIDIA` and `NOTICE` in that repo. Quantizing loses some quality, and nobody has measured how much for this model. The smallest file is 16.8 GiB, which is more than a T4's 14.56 GiB. So llama.cpp's `--fit on` keeps as much as fits on the GPU and leaves the rest of the MoE expert weights in system RAM. Only about 3.5B parameters are active per token, so this works, but it's slower than a full-GPU setup. This path hasn't been run here; the cells follow the GGUF card and llama.cpp's current flags.

We use **IQ4_XS (17.09 GiB, 4.62 bits per weight)**. It's about the size of Q2_K/Q3_K_S but keeps more precision per weight.

In Colab (Runtime → Change runtime type → **T4 GPU**), add `EVON_API_KEY` to Secrets (🔑) with the same value as your local `.env`. Turn on notebook access for it. Then run these cells:

**Cell 1: check GPU, RAM and disk.** You need about 14.5 GiB of VRAM, about 12 GB of RAM, and about 30 GB of free disk.
```python
!nvidia-smi --query-gpu=name,memory.total --format=csv
!free -g | head -2
!df -h / | tail -1
```

**Cell 2: build llama-server with CUDA for the T4 (sm_75).** This takes about 10–15 minutes, once per session.
```python
!git clone --depth 1 https://github.com/ggml-org/llama.cpp /content/llama.cpp
!cmake -S /content/llama.cpp -B /content/llama.cpp/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=75 -DLLAMA_CURL=OFF > /dev/null
!cmake --build /content/llama.cpp/build --config Release -j 4 --target llama-server 2>&1 | tail -3
!ls -la /content/llama.cpp/build/bin/llama-server
```

**Cell 3: download the model (17 GiB, about 5–10 minutes).**
```python
!pip install -q -U huggingface_hub
!hf download luminenio/gnani-evon-v3.3-30B-A3B-GGUF gnani-evon-v3.3-30B-A3B-IQ4_XS.gguf LICENSE LICENSE-NVIDIA NOTICE --local-dir /content/models
```

**Cell 4: start the server.** It binds to 127.0.0.1 only and requires the key. `--fit on` sizes the GPU/RAM split automatically.
```python
import subprocess
from google.colab import userdata
key = userdata.get("EVON_API_KEY")
log = open("/content/llama.log", "w")
srv = subprocess.Popen([
    "/content/llama.cpp/build/bin/llama-server",
    "-m", "/content/models/gnani-evon-v3.3-30B-A3B-IQ4_XS.gguf",
    "--alias", "model",            # must equal EVON_MODEL_NAME
    "--fit", "on",                 # put what fits on the T4, the remaining experts in RAM
    "-c", "8192", "-np", "1",      # our prompts are small; one slot saves memory
    "--jinja",                     # use the model's own chat template
    "--host", "127.0.0.1", "--port", "8000",
    "--api-key", key,
], stdout=log, stderr=subprocess.STDOUT)
```

**Cell 5: wait until it's ready, and show how the model was split.**
```python
import time, urllib.request
while True:
    if srv.poll() is not None:
        print(open("/content/llama.log").read()[-4000:]); raise SystemExit("llama-server exited, see log above")
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/v1/models", headers={"Authorization": f"Bearer {key}"})
        print(urllib.request.urlopen(req, timeout=5).read().decode()[:300]); break
    except Exception:
        time.sleep(10)
!grep -iE "offload|CUDA0 model buffer|CPU_Mapped|fit" /content/llama.log | tail -8
!nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

Then run **Cell 5 and Cell 6 from Option A** to open the HTTPS tunnel and test it. They're identical for both servers. If Option A's Cell 6 takes over ~60 s, the T4 is too slow for the backend's 90 s timeout. Lower `-c` to 4096 and make sure nothing else is using the GPU.

If it runs out of memory at startup, add `"--n-cpu-moe", "20"` (more experts in RAM) and remove `"--fit", "on"`. Raise 20 until it starts. If system RAM is the limit, try the smaller `Q3_K_S` file.

## 3. Connect your local backend

1. In the project-root `.env` on your laptop:
   ```
   EVON_MODE=remote
   EVON_BASE_URL=https://<printed-name>.trycloudflare.com/v1
   EVON_MODEL_NAME=model
   EVON_API_KEY=<the key from step 1>
   ```
   Don't use `localhost` here. On your laptop, localhost is your laptop, not the GPU.
2. Check the connection:
   ```powershell
   cd backend; .venv\Scripts\python -m app.evon
   ```
   You should see `"model_available": true`, then `OK in N s (remote): <Tamil sentence>`.
3. Restart the API (it reads `.env` at startup), and the worker too:
   ```powershell
   cd backend; .venv\Scripts\uvicorn app.main:app --port 8000
   cd backend; .venv\Scripts\python -m app.worker
   ```
4. In the app, go to **Settings → Service status → Check now**. It calls `GET /api/evon/health` through the backend.

## 4. When things go wrong

| You see | Meaning | Fix |
|---|---|---|
| `cannot reach the Evon server … tunnel URL changed` | The Colab session or tunnel is gone | Re-run Cells 3–5 and update `EVON_BASE_URL` |
| `Evon server unavailable (HTTP 530/502/503)` | The tunnel is up but vLLM is down or still loading | Check `/content/vllm.log`, or wait for Cell 4 |
| `rejected the API key` | The key differs between `.env` and Colab Secrets | Make them identical, then restart the API |
| `endpoint or model not found` | The URL is missing `/v1`, or the model name doesn't match | Use `…/v1` and `EVON_MODEL_NAME=model` |
| `request timed out` | The tunnel cut the request at 100 s, or the model is overloaded | Retry; keep `EVON_TIMEOUT` < 100 |
| `model returned invalid structured output` | The model's JSON failed validation twice | Retry; the task is kept in a safe state |

Failures are retried up to 3 times with backoff (honouring `Retry-After` on 429). Then the task moves to **Needs your help**, or the reply is kept for resending. The app never makes up a model answer. While the GPU is off, set `EVON_MODE=mock` to keep developing. Mock output is tagged `[Mock Evon: simulated, not a real model]` everywhere it appears.
