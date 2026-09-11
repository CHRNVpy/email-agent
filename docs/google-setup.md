# Google Cloud setup

The agent acts as one Google account (its mailbox, Drive and Sheets). Setup takes about 15 minutes.

## 1. Project and APIs

Create (or pick) a Google Cloud project and enable: **Gmail API, Google Drive API,
Google Docs API, Google Sheets API, Cloud Pub/Sub API**.

## 2. OAuth client for the agent account

1. *APIs & Services → OAuth consent screen*: internal (Workspace) or external with the agent
   account added as a test user.
2. *Credentials → Create credentials → OAuth client ID → Desktop app*. Download the JSON as
   `credentials.json` into the project root.
3. Authorise once (opens a consent URL; sign in **as the agent account**):

   ```bash
   python -m app.cli auth        # writes data/token.json, refreshed automatically afterwards
   ```

Scopes requested: `gmail.modify`, `spreadsheets`, `documents`, `drive`.

## 3. Gmail push notifications (Pub/Sub)

```bash
PROJECT=your-gcp-project
gcloud pubsub topics create gmail-agent --project $PROJECT

# Allow Gmail to publish to the topic
gcloud pubsub topics add-iam-policy-binding gmail-agent --project $PROJECT \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher

# Service account that signs push requests (the app verifies its JWT)
gcloud iam service-accounts create pubsub-push --project $PROJECT

gcloud pubsub subscriptions create gmail-agent-push --project $PROJECT \
  --topic=gmail-agent \
  --push-endpoint=https://agent.yourcompany.com/gmail/push \
  --push-auth-service-account=pubsub-push@$PROJECT.iam.gserviceaccount.com \
  --push-auth-token-audience=https://agent.yourcompany.com/gmail/push
```

Then set in `.env`:

```dotenv
GMAIL_PUBSUB_TOPIC=projects/your-gcp-project/topics/gmail-agent
PUBSUB_AUDIENCE=https://agent.yourcompany.com/gmail/push
PUBSUB_SERVICE_ACCOUNT=pubsub-push@your-gcp-project.iam.gserviceaccount.com
```

On start-up the service calls `users.watch` and renews it before the 7-day expiry.

**Local development:** expose port 8000 with a tunnel (ngrok, zrok, cloudflared) and point the
subscription at the tunnel URL:

```bash
gcloud pubsub subscriptions update gmail-agent-push --push-endpoint=https://<tunnel>/gmail/push
```

## 4. Drive change notifications (optional)

For a knowledge base that stays in sync with Drive:

```dotenv
DRIVE_FOLDER_IDS=folderId1,folderId2
DRIVE_WEBHOOK_URL=https://agent.yourcompany.com/drive/push
DRIVE_CHANNEL_TOKEN=<random string, verified on every notification>
```

```bash
python -m app.cli index drive     # initial full index
```

The service opens a `changes.watch` channel, renews it before it expires and re-indexes
only the files that changed inside the tracked folder trees. The webhook domain must be
served over HTTPS with a valid certificate.
