# Open Book — Client Project Plans

Live, client-facing project dashboards hosted on GitHub Pages. Each client gets a clean web page — shareable via URL, always current, no PDF needed.

**Live site:** [plans.teamopenbook.com](https://plans.teamopenbook.com)

---

## How it works

A GitHub Action runs **hourly on weekdays** and regenerates every client dashboard from live Monday.com data. Clients share one URL per project and always see fresh data.

```
Monday.com (data) → GitHub Action (hourly) → GitHub Pages (client views)
```

The Action:
1. Reads `clients.yaml` for the list of active clients
2. Queries Monday.com board `18397531209` for each client's group
3. Filters to client-visible items and subitems
4. Renders `_template/index.html` (Jinja2) with the data
5. Writes `{client-slug}/index.html`
6. Commits + pushes only if content changed

---

## Active clients

| Client | URL | Monday Group |
|---|---|---|
| Stony Brook School | [plans.teamopenbook.com/stony-brook](https://plans.teamopenbook.com/stony-brook) | `topics` |
| KeHE Partnership | [plans.teamopenbook.com/kehe](https://plans.teamopenbook.com/kehe) | `group_mm1t9gqj` |

---

## Repo structure

```
openbook-client-plans/
├── .github/workflows/
│   └── refresh-dashboards.yml    ← cron + manual trigger
├── _template/
│   └── index.html                ← Jinja2 template
├── scripts/
│   ├── refresh-dashboards.py     ← main script
│   └── requirements.txt          ← Python deps
├── clients.yaml                  ← client config
├── index.html                    ← root landing page
├── stony-brook/index.html        ← generated
├── kehe/index.html               ← generated
└── README.md
```

---

## Adding a new client

1. **Open `clients.yaml`** and add an entry:

```yaml
  - slug: new-client          # becomes the URL path
    display_name: New Client
    monday_group_id: group_xyz  # from Monday board URL
    kickoff_date: "2026-06-01"
    close_date: "2027-05-31"
    project_type: linear        # or "ongoing"
    phases:                     # for linear projects
      - Sign-to-Start
      - Know
      - Show
      - Build
      - Close-Out
```

2. **Commit and push.** The next Action run creates `new-client/index.html` automatically.

3. **Or trigger manually:** Actions tab → "Refresh Dashboards" → "Run workflow".

That's it. The folder is created, the dashboard is live within ~60 seconds.

### Project types

- **`linear`** — phased projects (e.g. Sign→Know→Show→Build→Close). List phase names in `phases:`. States derived from Monday data.
- **`ongoing`** — deliverable-based projects (e.g. KeHE). List major deliverable names in `deliverables:` matching Monday workstream names exactly.

---

## Monday.com setup for new clients

Each client needs a **group** on board `18397531209`. Within that group:

**Items** = Workstreams (e.g. "Visual Brand System", "Messaging Guide")
- Set the **Phase** column (`color_mkzzgws0`) for linear projects

**Subitems** = Tasks and milestones under each workstream
- Check **Visible to Client** (`boolean_mm18mjmm`) for items the client should see
- Check **Milestone?** (`boolean_mm00qyep`) for prominent milestones (vs smaller tasks)
- Set **Type** (`dropdown_mm0ekqzg`) to "Approval Needed" or "Client Meeting" for highlighted rows
- Add **Dashboard Notes** (`long_text_mm26ejw9`) for any notes shown on the dashboard
- Set the **Date** (`timerange_mm00qh4t`) for timeline display
- Mark the default **Status** column as "Done" when complete

---

## Running locally

```bash
cd openbook-client-plans
pip install -r scripts/requirements.txt
MONDAY_API_TOKEN=your_token python scripts/refresh-dashboards.py
```

The script prints progress for each client and writes files only when content changes.

---

## Manual refresh

Anyone with repo write access can trigger a refresh from the GitHub web UI:

**Actions** tab → **Refresh Dashboards** → **Run workflow**

---

## Secrets

| Secret | Where | Purpose |
|---|---|---|
| `MONDAY_API_TOKEN` | Repo → Settings → Secrets | Monday.com API access |
| `GITHUB_TOKEN` | Auto-provided by Actions | Commit + push |

The Monday token is **never** stored in code. Rotate quarterly via Monday → Developers → My Access Tokens.

---

## DNS

`plans.teamopenbook.com` → CNAME → `open-book-communications.github.io`

Domain verified via TXT record at `_github-pages-challenge-Open-Book-Communications.plans.teamopenbook.com`.

---

## Troubleshooting

| Issue | Fix |
|---|---|
| Dashboard shows stale data | Check Actions tab for failed runs. Trigger manual refresh. |
| Monday API errors | Rotate `MONDAY_API_TOKEN` in repo Secrets. |
| New client folder not created | Verify `slug` and `monday_group_id` in `clients.yaml`. Check Action logs. |
| Phase tracker stuck on "Upcoming" | Ensure workstreams have the Phase column set and subitems marked Visible to Client. |

GitHub sends failure emails automatically when the Action fails.

---

*Built April 2026 · Open Book Communications · Minneapolis, MN*
