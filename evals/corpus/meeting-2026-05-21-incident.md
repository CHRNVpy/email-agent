---
title: "Postmortem: EU ingestion outage"
date: 2026-05-21
---
On 19 May 2026 data ingestion in the EU region stopped for 3 hours and 12 minutes. Dashboards stayed available but showed stale data. No data was lost.

Root cause: the TLS certificate of the message queue broker expired, so the ingestion workers could not connect. Certificate renewal was a manual yearly task and the reminder went to a former employee.

Action items: automate certificate renewal with cert-manager (owner: Platform team, due 15 June); add an alert 30 days before any certificate expires; send affected Enterprise customers a written incident report.
