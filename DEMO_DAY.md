# Demo Day Runbook — CommerceOS

This branch is optimized for the Razorpay AI Buildathon Track 01 presentation without changing the working payment boundary.

Official Track 01 brief: https://razorpay.com/buildathon/

## One-line pitch

CommerceOS turns an existing merchant catalog into a website designed for AI buyers, then lets a grounded shopping agent discover products and move into a bounded, explicit Razorpay Test Mode purchase flow without giving the model direct control of money or inventory.

## What to make judges notice

1. **No merchant rebuild.** Start from the existing CSV, JSON, or SQLite source and publish a lossless normalized catalog.
2. **The AI gets its own web interface.** It navigates linked JSON pages instead of screenshots, brittle selectors, or a giant opaque tool list.
3. **The model recommends; deterministic code controls money.** Product selection is not consent. The backend revalidates live catalog facts, records an explicit spending ceiling, and only then creates a Razorpay order.
4. **Payment success is verified, not assumed.** Checkout callbacks are checked server-side against the stored Razorpay order, signature, live provider status, amount, and currency.
5. **Inventory changes only after verified captured payment.** Duplicate success paths are idempotent and verification outages keep the same purchase attempt retryable instead of inviting a double payment.
6. **Revenue is part of the agent behavior.** The shopping prompt now prefers concise comparisons, useful bundles, and one evidence-backed complementary recommendation instead of dumping the catalog.

## Five-minute demo

### 0:00–0:40 — Problem

Open **Overview**.

Say: “Existing e-commerce sites are built for human eyes. AI buyers either scrape HTML or need merchants to expose a large custom tool surface. CommerceOS gives the AI a first-class storefront of its own while the merchant keeps the data source they already have.”

Point at the readiness, record count, linked agent endpoint, and Track 01 trust strip.

### 0:40–1:30 — Agent-native storefront

Open **Agent API**.

Show the Store Home JSON first, then UCP profile. Explain that every next move is advertised in the current page and the LangGraph browser is allowed to execute only those advertised transitions.

Do not spend time reading raw JSON. The point is that the AI has a deterministic navigational surface.

### 1:30–3:15 — AI buyer

Open **AI Buyer** and use a decision-oriented request instead of “show all products”. Good prompts:

- `I need something for a weekend hike. Compare the strongest options and tell me what you would pick.`
- `Build me a small hiking setup from this store and keep the choices practical.`
- `I want a good value option. Compare a few choices instead of listing the whole catalog.`

Select one or more grounded product cards. Change quantity once so the UI makes it obvious that selection is a local shopper action, not a stock mutation.

### 3:15–4:20 — Bounded money action

Click **Buy now** → **Review purchase**.

Call out the explicit spend gate: the backend rebuilds the purchase from canonical record IDs and current price/stock, then the user authorizes a maximum amount for this attempt. Only after that does Razorpay Standard Checkout open in Test Mode.

Complete the test payment if credentials are configured.

### 4:20–5:00 — Failure + audit story

Show one of these depending on the live state:

- Close Checkout: purchase remains unpaid and inventory is unchanged.
- If verification is temporarily unavailable: the UI says **do not pay again** and exposes **Retry payment confirmation** on the same attempt.
- Show **Audit / Sync** and explain that catalog revisions are immutable publications; failed syncs leave the previous good revision active.

Finish with: “The AI is flexible where judgment helps, and deterministic everywhere money, catalog truth, and inventory need guarantees.”

## Architecture sentence

Merchant source → deterministic normalization → MongoDB revisioned catalog → linked JSON/UCP storefront → bounded LangGraph browser → human purchase review → deterministic authorization policy → Razorpay Test Checkout → server-side verification → idempotent fulfillment.

## Do not claim

- Do not call Standard Checkout “Razorpay Agentic Payments” or UPI Reserve Pay. Those are separate early-access capabilities.
- Do not claim the model itself charges money or mutates stock.
- Do not claim production authentication; the current operator key is an interim boundary.
- Do not invent conversion or revenue percentages. The revenue-growth feature is the recommendation/bundle/cross-sell behavior and the reduction of AI shopping friction.

## Last checks before recording

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m pytest
```

Then test the exact five-minute path once with the same catalog and model/provider you will use in the recording.
