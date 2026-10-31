# CLAUDE.md --- Dynamic Multilingual AI Calling Agent

## 1. Project Overview

Build a **web application (installable PWA) powered by a dynamic,
multilingual AI calling agent**. A native mobile app comes later, once
the product is stable (see Section 9). A user explains a real-world communication task in
their preferred language. The agent understands the goal, contacts the
relevant person by phone, communicates in that person's preferred
language, asks intelligent follow-up questions, and reports the outcome
to the user in their language.

The bus-conductor scenario in this document is an illustrative example
only. **Do not build a bus-specific application or hardcode bus-related
workflows.** The system must be goal-driven and reusable across many
supported conversational tasks.

The product's defining objective is to reduce the effort required from
both people while helping the user complete communication tasks across
language barriers.

## 2. Strict Feature Scope

The product has **exactly four core features**:

1.  Adaptive conversations and intelligent follow-up.
2.  Persistent task memory and recovery.
3.  Proactive notifications and conditional follow-ups.
4.  A human-in-the-loop approval system.

Do not add unrelated product features or expand the product scope
without explicit instruction. The web/PWA UI, backend, database, model
integrations, telephony, and notification infrastructure are
implementation components required to support these four features and
the multilingual calling workflow---not additional product features.

## 3. Core User Experience

### Requester (Person 1)

On initial setup or when relevant information is missing, collect only
the necessary information: - Preferred language for speaking with the
agent and receiving results. - The goal or task the user wants
completed. - The target person's contact details, such as name and phone
number, or a selection from saved contacts. - Task-specific constraints
or context required to perform the request. - Calling and approval
preference, including whether the user requires approval before each
call.

Ask questions conversationally, preferably one at a time. Do not make
users complete a long questionnaire. Save appropriate preferences
securely and reuse them. Do not ask for information already available
unless confirmation is necessary.

### Contact / Recipient (Person 2)

When contacting a person, the agent should: - Clearly identify itself as
an AI assistant calling on behalf of the requester. - Briefly explain
the reason for the call. - Ask which language the recipient prefers, if
not already known. - Check whether it is a convenient time to speak and
whether the person is able to help. - Ask only questions relevant to the
current task. - Respect refusal, requests to stop, and applicable
consent requirements.

Do not require every recipient to register or answer a fixed
questionnaire. Keep the interaction brief and context-sensitive.

## 4. Feature One --- Adaptive Conversations and Intelligent Follow-up

The agent must not simply translate the user's first question word for
word. It should understand the **intended outcome**, track the
information needed to achieve it, interpret the other person's answers,
and ask useful follow-up questions when information is missing, vague,
contradictory, or ambiguous.

Requirements: - Convert the request into a structured task goal and a
list of required facts or outcomes. - Generate questions dynamically
from the goal and current conversation---not from hardcoded scenarios. -
Interpret answers in context. - Detect when an answer does not
sufficiently address the question. - Ask a targeted clarification only
when it materially helps complete the task. - Stop asking once the
required information is sufficiently complete. - Distinguish confirmed
facts, estimates, unanswered questions, and uncertain information. - Do
not fabricate an answer or claim the task succeeded without evidence. -
Keep the conversation natural, concise, and in the recipient's preferred
supported language.

Example (illustrative, not a special-case implementation): - The user
asks when a booked bus will reach their stop. - The conductor says, "The
bus is nearby." - The agent recognizes that "nearby" does not answer the
arrival-time question and asks in Tamil, "Approximately how many minutes
will it take?" - The result is then reported in the requester's
preferred language.

The same reasoning pattern should work for different supported tasks,
such as checking a delivery, asking about an appointment, or clarifying
a service inquiry, when the required contact and integrations are
available.

## 5. Feature Two --- Persistent Task Memory and Recovery

The agent must save task state outside the language model's transient
conversation context.

Persist, as appropriate: - Unique task ID and requester ID. - Original
user request and normalized task goal. - Requester's preferred language
and recipient's known/preferred language. - Contact details used for the
task and the source/confirmation status of those details. - Current task
state. - Questions asked, answers received, and evidence/confirmation
status. - Required information still unresolved. - Last confirmed
checkpoint and the next safe action. - Approval requirements, approval
status, and relevant audit events. - Follow-up time, if any, and whether
the user authorized the follow-up. - Timestamps, call attempts,
outcomes, and failure reasons. - User preferences that are useful for
future tasks, such as default language and call-approval settings.

Requirements: - Resume from the last confirmed checkpoint after
interruption or disconnection. - Do not restart the entire conversation
unnecessarily. - Do not repeat completed steps unless verification is
needed. - Prevent duplicate calls and duplicate actions using task IDs,
idempotency keys, and explicit state transitions. - Do not assume an
action succeeded merely because it was requested; record confirmed
outcomes. - Apply retention limits and allow appropriate deletion of
stored task data. - Protect personal and call-related information with
authentication, authorization, secure storage, and minimal data
collection.

A task state machine should include states similar to: `NEW`,
`PLANNING`, `AWAITING_APPROVAL`, `CALLING`, `CLARIFYING`, `VERIFYING`,
`COMPLETED`, `NEEDS_HUMAN_HELP`, and `FAILED`.

Use only the states needed by the implementation, but make state
transitions explicit and validated.

## 6. Feature Three --- Proactive Notifications and Conditional Follow-ups

Notify the requester when a meaningful event occurs, including: - Call
failed or was disconnected. - An answer was received. - The task
completed or could not be completed. - User approval or clarification is
needed. - An agreed follow-up time has arrived.

Requirements: - Store follow-up deadlines and task state persistently. -
Use a scheduler/worker for scheduled checks; do not rely on an open
mobile app or an in-memory timer. - When appropriate, remind the user
and ask whether they want the agent to follow up. - Only place follow-up
calls when the required authorization and approval policy allow it. -
Apply bounded retry rules, backoff where appropriate, and
duplicate-prevention safeguards. - Respect cancellation, opt-out, quiet
periods, and stop requests. - Never repeatedly call someone without
appropriate authorization. - Notifications must summarize the actual
state and must not claim success without confirmation.

Example: A supplier says delivery is expected by 5 PM. The agent records
the promise and time. At the appropriate time it notifies the requester
and, according to the user's configured policy, asks whether to contact
the supplier again. It does not blindly call repeatedly.

## 7. Feature Four --- Human-in-the-Loop Approval System

The agent must pause for human judgment when necessary, including: - The
contact identity is uncertain or multiple contacts match. - The agent
cannot confidently interpret a request or proposed next action. -
Sensitive personal information is requested or may be disclosed. - A
proposed action could create a financial commitment, booking change,
contractual commitment, or other significant consequence. - The user's
preferences require approval before every call. - A task reaches a
policy boundary or needs the requester to resolve an ambiguity.

The requester must be able to: - Approve the proposed action. - Reject
it. - Edit the question or instruction. - Cancel or stop the task. -
Take over manually when appropriate.

Implementation requirements: - Enforce approval in backend permission
logic, not only in the UI or model prompt. - Record who approved what
and when. - Bind approvals to the specific task/action they authorize;
do not reuse an old approval for a materially different action. - If
approval is denied or unavailable, pause safely and explain the state. -
Never allow the reasoning model to bypass approval controls.

## 8. Dynamic, Goal-Driven Design

The agent must not be hardcoded for buses, suppliers, appointments, or
any single domain.

For every new request, the task engine should: 1. Understand the user's
intended outcome. 2. Identify the minimum information required to
achieve it. 3. Check saved preferences and supplied contact details. 4.
Ask for missing information only. 5. Determine a safe, permitted next
action. 6. Conduct a context-aware conversation in the recipient's
supported language. 7. Track required facts and ask follow-up questions
as needed. 8. Verify whether the goal has been achieved. 9. Save the
state and schedule a follow-up only when appropriate and authorized. 10.
Return a concise result in the requester's preferred language.

Represent tasks as structured data, for example: - `goal` - `contact` -
`requester_language` - `recipient_language` - `required_information` -
`known_information` - `unresolved_questions` - `approval_policy` -
`current_state` - `next_action` - `result` - `follow_up_at`

Use a general task engine with model-assisted planning and deterministic
validation/permission rules. Avoid brittle chains of `if bus ...` or
domain-specific scripts. Use task-specific adapters only when a real
external system is needed.

The agent is not expected to perform literally every possible task. Its
capabilities are limited by available tools, contact access, telephony
services, language support, recipient cooperation, user permissions, and
applicable policies. If a task cannot be performed with available
capabilities, explain the limitation and offer a safe next step rather
than pretending it can be completed.

## 9. Web Application (PWA)

**For now, build a responsive website delivered as a Progressive Web
App (PWA), not a native mobile app.** A website is faster to iterate on
while the product is still changing. Once the workflow and features are
stable, the client will be moved to a native mobile app (React Native is
the likely choice, since it reuses React knowledge).

PWA requirements: - Mobile-first, responsive layout that works well on
phone browsers as well as desktop. - Web app manifest and service worker
so it is installable on Android/iOS home screens. - Web Push
notifications (via the service worker) for task events and follow-ups;
note that iOS supports Web Push only for installed PWAs (iOS 16.4+). -
Microphone access via browser APIs for voice request entry; requires
HTTPS. - Graceful offline/loading states; the service worker may cache
the app shell but must not cache private task data insecurely.

Build with React (e.g. Vite + React) so components and logic carry over
to React Native later. Keep API calls, types, and state logic separate
from web-only UI code to make the later mobile migration easier. Do not
start the native mobile app until explicitly instructed.

Keep the web experience focused on the four core features: - Request
entry by voice (and optionally text as an accessible fallback). - Task
summary and approval actions. - Current task state and
conversation/result summary. - Notifications and follow-up decisions. -
Preferences needed for language, permissions, and task memory.

Potential screens: - Onboarding/preferences. - Voice/task entry. - Task
confirmation or approval. - Active task status. - Task
details/history. - Notifications and follow-up. - Settings for language
and approval preferences.

Avoid adding unrelated modules. Make the app usable by people who prefer
speaking rather than typing.

The rest of this document refers to the "mobile app" or "mobile
client"; until the mobile migration, read these as the web/PWA client.

## 10. Backend and Suggested Architecture

Suggested architecture: - **Web client (PWA):** React (e.g. Vite +
React) with a manifest and service worker; React Native mobile client
later. - **Backend
API:** Python + FastAPI, unless an existing project structure makes
another choice more appropriate. - **Database:** A persistent relational
database such as PostgreSQL for users, preferences, tasks, checkpoints,
approvals, call records, and follow-up jobs. - **Background
worker/scheduler:** A durable queue or scheduled worker for
notifications and conditional follow-ups. - **Telephony provider:** A
compatible provider/API capable of initiating real phone calls and
supporting the required audio flow. - **Push notifications:** Web Push (VAPID)
through the PWA service worker for now; a mobile push service later. - **AI model APIs:** Integrate the
three required Gnani AI models after verifying their official API
documentation and access.

The backend is responsible for task orchestration, state transitions,
permissions, persistent memory, idempotency, retries, scheduling, and
audit records. The web/PWA app is a client and must not hold provider
secrets or make authorization decisions by itself.

## 11. Required Gnani AI Models

The hackathon provides 5,000 free credits and requires use of: 1.
**Prisma v2.5** 2. **Timbre v2.5** 3. **Evon v3.3**

Intended roles in the proposed architecture: - **Prisma v2.5:** Speech
recognition/transcription, if supported by its official API. - **Evon
v3.3:** Task interpretation, reasoning, planning, and follow-up question
generation, if supported. - **Timbre v2.5:** Speech
generation/text-to-speech, if supported.

These roles are proposed mappings and must be verified against the
actual model/API documentation. Do not invent endpoints, parameters,
model capabilities, supported languages, or credit costs. Keep model
calls behind a small adapter/service layer so integrations can be tested
and changed safely.

The models do not automatically provide ordinary phone calling,
persistent storage, scheduled jobs, or mobile push notifications. Those
require separate components.

## 12. Telephony and Conversation Requirements

-   Verify whether the selected telephony provider supports the required
    outgoing calls, audio streaming, and call status events.
-   Verify the actual ability to pass speech audio between the telephony
    service and the AI pipeline.
-   Handle no answer, busy, rejected call, disconnection, poor audio,
    and unsupported language.
-   Use bounded call attempts and avoid accidental duplicate calls.
-   Identify the agent as AI and explain the purpose of the call.
-   Respect consent, opt-outs, local law, and provider policies.
-   Do not record or retain audio/transcripts unless necessary and
    appropriately disclosed.
-   Keep personal information minimal and secure.
-   Provide a way to cancel/stop a running task.
-   Do not claim real-time or fully autonomous calling works until it
    has been tested end to end.

If telephony access or model access is not yet available, first build
and test the workflow with a clearly labeled simulated-call adapter.
Keep the telephony interface replaceable so a real provider can be
connected later. A simulation must never be presented as a real call.

## 13. Error Handling and Reliability

Handle at least: - Speech transcription failure or low confidence. -
Unsupported language or voice. - Missing/invalid contact information. -
Contact ambiguity. - No answer, busy, rejected, or disconnected calls. -
Recipient refusal or request to stop. - Incomplete or contradictory
answers. - Model/API timeout, rate limit, or failure. -
Database/scheduler/notification failure. - Missing approval or rejected
approval. - Follow-up cancellation and duplicate-job attempts.

Save a safe checkpoint after meaningful progress. Use timeouts, bounded
retries, idempotency, structured logs, and clear task states. Surface
uncertainty to the user rather than making up facts.

## 14. Privacy, Safety, and Permission Rules

-   Authenticate users and authorize access to their own tasks and
    contacts.
-   Store secrets only on the server in environment variables or a
    secrets manager.
-   Never commit API keys, phone numbers, credentials, or private call
    data to source control.
-   Encrypt traffic and use secure storage for sensitive data.
-   Collect only the data needed for the task and define
    retention/deletion behavior.
-   Make the AI identity and call purpose clear to the recipient.
-   Respect applicable calling, privacy, consent, and telephony rules.
-   Require approval for uncertain contacts and consequential actions
    according to policy.
-   Provide cancellation and opt-out handling.
-   Do not bypass consent, approval, or safety controls to maximize
    automation.

## 15. Development Plan

Implement in small, testable stages:

### Phase 1 --- Inspect and plan

-   Inspect the existing repository before changing it.
-   Identify current stack, code structure, setup instructions, and
    existing functionality.
-   Check official Gnani API documentation and credentials/access.
-   Record assumptions and blockers; do not fabricate API details.

### Phase 2 --- Task engine and simulated workflow

-   Define the task data model and state machine.
-   Implement goal parsing/planning behind an adapter.
-   Implement required-information tracking and adaptive follow-up.
-   Build a simulated call flow to demonstrate both languages and
    incomplete-answer handling.
-   Ensure simulation is clearly labeled.

### Phase 3 --- Persistence and approval

-   Persist task checkpoints and preferences.
-   Implement resume/recovery and duplicate-action prevention.
-   Implement backend-enforced approvals, rejection, editing,
    cancellation, and audit history.

### Phase 4 --- Notifications and conditional follow-ups

-   Add durable scheduled jobs.
-   Implement task event notifications.
-   Add user-authorized follow-up decisions, bounded retries, and
    cancellation.

### Phase 5 --- Web app (PWA)

-   Build the web/PWA flows required for task entry, approvals,
    progress, results, and preferences.
-   Add manifest, service worker, and Web Push.
-   Connect the app securely to the backend.
-   Handle offline/loading/error states.
-   Native mobile app is a later phase, only after the web version is
    stable and when explicitly requested.

### Phase 6 --- Gnani models and real telephony

-   Integrate Prisma v2.5, Evon v3.3, and Timbre v2.5 based on verified
    official API documentation.
-   Add a compatible telephony provider and real audio/call status
    integration.
-   Measure latency, transcription quality, language quality, task
    completion, and credit usage.
-   Preserve a simulated mode for repeatable tests.

### Phase 7 --- End-to-end testing

Test a range of tasks rather than only the bus example. Include: -
Complete answers and vague answers requiring clarification. - Different
requester and recipient languages. - Missing contact details. -
Ambiguous contacts requiring approval. - Rejected approval and
cancellation. - Call disconnection and recovery. - Follow-up reminders
and cancelled follow-ups. - Duplicate prevention and model/provider
failures. - Recipient refusal and unsupported language. - Clear
distinction between confirmed results and estimates.

## 16. Acceptance Criteria

The project is ready for a demo when: - A user can describe a supported
task in their preferred language. - The system identifies the goal and
asks only for missing essential information. - The agent conducts a
context-aware conversation and follows up on incomplete answers. - The
result is summarized in the requester's language, with uncertainty
stated honestly. - Task state survives backend restarts and interrupted
calls. - Resuming a task does not repeat completed steps or trigger
duplicate calls. - Notifications reflect actual task events and
follow-up permissions. - Approval is enforced on the backend and cannot
be bypassed by the model. - The same task engine handles multiple task
types without hardcoded domain-specific workflows. - Real calls are only
claimed when a real telephony integration is configured and tested. -
Secrets and personal information are not exposed in logs, client
bundles, or source control.

## 17. Instructions for Claude Code

When working in this repository: 1. Read this file first, then inspect
the actual codebase and existing documentation. 2. Do not assume files,
frameworks, APIs, credentials, or integrations exist without checking.
3. Before implementing, provide a concise summary of the current
repository state and a staged implementation plan. 4. Preserve working
functionality and follow existing project conventions. 5. Prefer small,
maintainable changes with clear interfaces, typed data models where
practical, and tests. 6. Keep the four-feature scope strict; do not add
unrelated product features. 7. Do not hardcode the bus example or any
single task domain. 8. Do not invent Gnani API endpoints or
capabilities. Verify documentation or clearly isolate placeholders
behind adapters. 9. Never expose secrets or put them in client-side
code. 10. Implement real authorization, durable task state, and error
handling---not just UI mockups. 11. Label simulated calls and mock
results clearly. 12. Explain how to configure, run, test, and demo each
completed stage. 13. When something is blocked by missing credentials,
provider access, or an unsupported capability, state the blocker and
implement a testable interface or simulation where useful. 14. Do not
claim a feature is complete until it has been implemented and tested.

## Final Product Definition

Build one reusable, dynamic multilingual AI calling agent inside a web
application (installable PWA), to be moved to a native mobile app later. The user states a goal; the agent obtains only
missing information, communicates with the relevant person in their
supported language, asks intelligent follow-up questions, remembers task
progress, notifies the user when needed, and pauses for approval when
required. The system must remain focused on the four specified core
features and must not be hardcoded to the bus scenario.
