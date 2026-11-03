# Bolo — One-Page Solution Pitch
### *Autonomous Multilingual Voice Calling Agent for Real-World Task Execution*

---

### Executive Summary
**Bolo** is a goal-driven, multilingual AI voice calling agent engineered to eliminate language barriers and everyday coordination friction across India. A user speaks a real-world request in their mother tongue (e.g., *"Find out when the bus reaches my stop"* or *"Check if my package will arrive today"*). Bolo plans the facts it needs, places a phone call, converses fluently in the recipient's preferred regional language, intelligently clarifies ambiguities, verifies details with read-backs, and reports verified facts back to the requester in their language.

---

### The Problem
* **Linguistic Fragmentation:** India spans hundreds of dialects, with 90%+ of non-English speakers preferring voice in regional languages. Cross-lingual phone coordination causes severe miscommunication.
* **Repetitive Coordination Burden:** Everyday logistics, transit checks, supplier updates, and clinic appointments consume valuable time with manual phone tag.
* **Brittle Automation:** Existing IVRs and chatbots rely on hardcoded decision trees. They fail when human answers are vague (*"the bus is nearby"*), cannot conduct natural clarifications, and hallucinate unverified conclusions.

---

### The Solution: Goal-Driven Voice Autonomy
Bolo replaces brittle scripts with a universal, adaptive task engine backed by **Gnani.ai Voice AI** and **Evon/LLM Reasoning**:
1. **Adaptive Dialogue & Clarification:** Dynamically plans required facts, detects vague or conflicting replies, and asks targeted follow-ups (e.g., *"Nearby? Approximately how many minutes?"*). Categorizes facts with evidence quotes as `confirmed`, `estimate`, `uncertain`, or `refused`.
2. **Deterministic Safety ("Model Proposes, Code Decides"):** AI outputs are constrained to strict Pydantic schemas. State transitions, call placement, and commitment gates are enforced deterministically by the task engine.
3. **Human-in-the-Loop Governance:** Financial commitments, bookings, sensitive data disclosure, or policy thresholds trigger a cryptographic one-time action approval (`SHA-256`) gate.
4. **Persistent Memory & Fault Recovery:** Checkpointed after every turn. Interrupted calls resume seamlessly without repeating answered questions; strict idempotency prevents duplicate calls.
5. **Proactive Async Worker:** Background jobs manage scheduled callbacks (e.g., *"Supplier promised 5 PM"*), retry logic, and instant Web Push notifications to the requester.

---

### Architecture & Technology Stack
| Layer | Technologies & Capabilities |
| :--- | :--- |
| **Speech Layer** | **Gnani Prisma v2.5** (STT) + **Gnani Timbre v2.5** (TTS), supporting 10 Indian languages with gender-matched native voices (Hindi, Tamil, Telugu, Gujarati, Kannada, etc.). |
| **Reasoning Engine** | **Evon v3.3** (or Claude) with JSON schema validation, automatic repair rounds, and prompt-injected voice gender grammar alignment. |
| **Core Backend** | **FastAPI (Python)** state machine, durable SQLite/PostgreSQL storage, background worker for async jobs and VAPID Web Push alerts. |
| **Client PWA** | Mobile-first **React + TypeScript (Vite)** installable PWA with microphone audio streaming (16 kHz WAV) and Service Worker push handling. |

---

### Business Value & Impact
* **Zero Language Barrier:** Seamless communication across 10 major Indian languages without human translators.
* **Enterprise & Consumer Versatility:** One unified engine handles logistics, appointment scheduling, supply-chain checks, and personal calling tasks.
* **Auditability & Trust:** Every call preserves full conversational transcripts, audio recordings, evidence citations, and user-approved checkpoints.
