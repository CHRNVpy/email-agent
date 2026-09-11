# Demo stand

Everything here is fictional: **Brightwave Labs**, a small B2B analytics company, with a CRM
(customers, orders, deals, invoices), a knowledge base of policies and meeting notes
(`evals/corpus`) and a sample contract. Use it to try the agent and to take screenshots.

## 1. Set up (≈10 minutes)

```bash
cp demo/.env.demo .env                     # fill in AGENT_EMAIL, ALLOWED_SENDERS, GEMINI_API_KEYS
docker compose up -d qdrant                # vector database
python -m demo.seed_crm                    # data/demo/crm.db — 24 customers, orders, deals, invoices
python -m app.cli init                     # app database + roles + ADMIN_EMAIL as admin
python -m app.cli index files evals/corpus # 20 policy / meeting-note documents -> Qdrant
```

**Try without Gmail first:**

```bash
python -m app.cli ask "Who are our top 5 customers by revenue this quarter?" --stats
```

`--stats` prints the plan, time per stage, tokens and cost.

**Google Sheets demo:** create a spreadsheet named **Brightwave demo workbook** in the agent's
Drive with two tabs, **Pipeline** and **Collections**, and import the headers from
`demo/workbook-pipeline.csv` and `demo/workbook-collections.csv`.

**Gmail (no Pub/Sub needed):** authorise the agent account once, then let the agent poll its inbox:

```bash
python -m app.cli auth    # sign in as the agent account (OAuth "Desktop app" client in credentials.json)
python -m app.cli poll    # checks the inbox every 15 s and replies in-thread; Ctrl+C to stop
```

Pub/Sub push (instant, for production) is described in [docs/google-setup.md](../docs/google-setup.md).

## 2. Demo emails

Send these from the `ALLOWED_SENDERS` address to the agent. Each one shows a different capability.

| # | Subject | Body | Shows |
|---|---|---|---|
| 1 | Top customers this quarter | Hi! Who are our top 5 customers by revenue this quarter? Please include the account manager for each. | SQL → formatted table |
| 2 | Overdue invoices → Collections | Find all invoices more than 30 days overdue and add them to the Collections tab of the Brightwave demo workbook (customer, invoice ID, amount, days overdue). Paste the sheet link. | chain `sql → sheets` |
| 3 | Refund question | A customer on an annual plan wants to cancel after 6 weeks. Can we refund them, and who needs to approve a $2,400 refund? | RAG over two documents, with citations |
| 4 | Contract review | Please summarise the attached MSA and list the termination clauses with their notice periods. *(attach `demo/contract.pdf`)* | reading an attachment |
| 5 | Update | Can you update it? | asks a clarifying question instead of guessing |

Optional: add `-model:grok-4` to a subject to answer with another model (needs `XAI_API_KEY`).

## 3. Screenshots

The strongest image for the README and Upwork is **email #1 or #2**: the request and the
reply with its table in one Gmail thread.

- Use Gmail on the web with the light theme and the browser window about 1400 px wide. Open the
  thread, expand both messages and close the side panels.
- Crop to the thread only. If the addresses are personal, blur them or use a demo account
  (for example *brightwave.agent@…*).
- Save as PNG to `docs/images/` and name the files after the scenario, e.g. `thread-top-customers.png`.
