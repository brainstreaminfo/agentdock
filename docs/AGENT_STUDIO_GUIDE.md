# Agent Studio guide: build and publish an agent without code

The Studio is at **/studio/** and only staff can open it. To make yourself staff,
run `make superuser`, or tick "Staff status" for your user in Django Admin.

To test agents you need a store: connect your own **development store** (see the
README). The playground and tests only run on stores you are a member of.

---

## 1. Start

- **Agents → Create** with a name, or
- **Clone** one of the 3 templates (Product Description Writer, Low Stock Alert,
  Sales Q&A Analyst). Templates are read-only examples; cloning gives you an
  editable copy.

Everything you edit goes into the agent's **draft**. Merchants never see the draft;
they only see **published versions** (v1, v2…).

## 2. Build: the seven sections

| Section | What you set | Tips |
|---|---|---|
| **Listing** | Name, icon (any name from lucide.dev/icons), tagline, description, "what it can do" bullets, example prompts, screenshots | Example prompts become buttons in chat. Listing changes apply immediately. |
| **Brain** | The instructions (system prompt), model, thinking effort, max steps and tokens | Click the variable chips to insert `{{shop.name}}`, `{{memory}}`, `{{settings.tone}}`… The right side shows the exact prompt the model receives. |
| **Tools** | What the agent may do in the store | Read tools are safe; write tools create changes that need approval. The required Shopify permissions are calculated for you. |
| **Merchant settings** | The form merchants fill in when installing | Add fields (text, number, choose one, toggle, product picker…). Use them in instructions as `{{settings.key}}`. |
| **Triggers** | Chat, a schedule, Shopify events | Schedules run in each shop's timezone. Each event adds the permission it needs. |
| **Behaviour** | Default autonomy and which levels merchants may choose | *Suggest* and *Ask before changing* always wait for approval; *Act automatically* applies changes directly (still undoable). |
| **Availability** | Visibility, plans, extra price, featured, auto-upgrade | *Private* agents are only visible to the shops you select. |

**Writing good instructions**
- Say what the agent is for in one sentence, then the steps it should follow.
- Name the tools it should use for each step (e.g. "find products with
  products.search, then read each with products.get").
- Say what *not* to do ("never invent specs", "don't create an alert if nothing
  is low").
- Keep it short; the platform already adds rules about approvals, safety and
  formatting.

## 3. Try it: Playground

**Playground → pick your store, the version (draft) and settings → Start chat.**
You'll see answers stream in, which tools were called, and approval cards for
changes. Under each answer, **Details** opens the full trace: every model turn
with its reasoning summary, every tool call with input and output, tokens and
cost.

Changes are **never applied automatically in the playground**, even for an
"Act automatically" agent. Approve a card if you really want to apply it to your
dev store.

For scheduled agents, use **Simulate a scheduled run** to run the draft once as if
the schedule fired.

## 4. Protect it: Tests

**Tests → Add test**: a prompt merchants might send, plus checks:

- *Must call* / *Must not call* a tool (e.g. must call `products.search`, must not
  call `products.update`)
- *Reply contains* / *doesn't contain* some text
- *Max cost* per run

**Run all tests** runs every test against the current draft on the store you pick.
Store changes are never applied during tests. Results belong to the exact draft
they ran on: edit the draft and they show "not run on this draft" again.

Tests call the real AI and cost money (a few cents each).

## 5. Ship it: Publish

**Publish** shows:
- what changed since the last version;
- whether the tests passed on this draft;
- **new permissions**. Shops that haven't granted them stay on the old version
  until the merchant approves the update.

Write a changelog and click **Publish**. Then:
- the draft becomes a new immutable version;
- shops that already granted all permissions are **upgraded automatically**
  (unless you switched off auto-upgrade under Availability).

## 6. Roll back: Versions

**Versions → Restore as draft** on an older version, then **Publish**. The old
behaviour comes back as a new version number (v3 = v1's content), so the history
stays honest.

## 7. Watch it: Analytics

Installs, active shops, runs per day (succeeded/failed), error rate and average
cost per run, plus the latest runs with links to their traces. The Studio
dashboard shows the same numbers for all agents.

## 8. What merchants see

- **Marketplace:** the listing (icon, tagline, description, screenshots,
  "what it can do", example prompts) and a plain-language list of permissions,
  calculated from the tools and events you picked. *Featured* agents get their
  own row. Visibility decides who sees it: Public, Beta (everyone, with a badge),
  or Private (only the shops you select under Availability).
- **Install:** if the store hasn't granted a permission yet, Shopify asks the
  merchant first. Then they fill in your **Merchant settings** form (product and
  collection fields become search pickers) and choose an autonomy level from
  the ones you allowed.
- **Schedule and events:** a cron schedule runs in the store's own timezone; the
  merchant can switch it off. Event triggers start a run when the event happens
  in their store.
- **My Team chat:** every installed agent with chat switched on joins the
  merchant's team. My Team picks an agent from its **tagline and description**,
  so say clearly what the agent is good at.
- **Updates:** see section 5. If the new version needs new permissions, the
  merchant sees "Update available" with the list and approves it in Shopify.

---

### Tools that don't exist yet

Studio → **Tool catalog → Custom GraphQL tool**: write one Shopify GraphQL query or
mutation, describe its inputs, and declare its permissions. It's checked against
Shopify's real schema before saving. Then add it to agents like any other tool.
Developers can add built-in tools in Python; see `docs/TOOL_CATALOG.md`.
