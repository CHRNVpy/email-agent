---
title: "Spec: Anomaly Alerts"
date: 2026-03-05
---
Anomaly Alerts watch key metrics and notify users when a value moves outside its expected range. The expected range comes from a seasonal forecast with a configurable sensitivity (low, medium, high).

Alerts can go to email, Slack and Microsoft Teams, or to any HTTPS endpoint via webhooks. Each alert includes the metric chart and the three dimensions that contributed most to the change.

Plan availability: Growth and Enterprise. Timeline: private beta in April 2026, general availability targeted for June 2026. Success metric: 40% of Growth accounts have at least one alert configured within 60 days of GA.
