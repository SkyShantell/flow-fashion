# Flow Try-On Factory

Standalone Streamlit bulk try-on pipeline.

## Pipeline

1. Choose/upload one avatar.
2. Paste up to 10 TikTok Shop product links.
3. SociaVault imports listing + review images.
4. Avatar is uploaded to Google Flow once and is always `reference_1`.
5. Product photos are uploaded as later references.
6. Google Flow `nano-banana-2` generates one 9:16 try-on image per product.
7. In Review mode, approve/regenerate images before video. In Full Auto, completed images automatically continue.
8. Google Flow `omni-flash` (Omni 1.1 Flash) generates 8-second 720p portrait video using the generated try-on image as `startImage`.
9. Refresh all video jobs, preview results, and download ZIPs.

## Streamlit secrets

Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml` and fill in:

- `USEAPI_TOKEN`
- `SOCIAVAULT_API_KEY`
- `GOOGLE_FLOW_EMAIL` (recommended when more than one Flow account is configured)
- optional `APP_PASSWORD`

Do not commit your real `secrets.toml`.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy

Push this folder to its own GitHub repository and deploy that repository as a separate Streamlit app. Set the secrets in the Streamlit Cloud app settings rather than committing them.

## UI V2

This bundle includes the redesigned dashboard UI:

- readable dark theme with explicit high-contrast form controls
- setup, avatar, pipeline mode, and scene controls moved to the sidebar
- separate Products / Generate / Results workspaces
- one-product-at-a-time reference editing instead of long accordions
- compact generation metrics and status table
- one-product-at-a-time result review with image/video side-by-side
- batch downloads kept in the Results workspace

The generation/API pipeline is unchanged.

## Video result behavior

This UI version automatically polls async Omni 1.1 Flash jobs after submission. The app waits for `created` / `started` jobs to reach `completed`, then loads the returned MP4 in the Results view and exposes Download MP4 plus the signed original URL when available. If a generation outlasts the wait window, the job ID is retained and **Check status** / **Check all video statuses** resumes retrieval later.

## R5 recovery + contrast update
- Adds a visible **Recover existing Flow videos** panel even when no products are imported.
- **Find recent Flow jobs** discovers executing jobs plus recent job history from useapi.
- Direct **Job ID lookup** can reopen a known useapi Flow job and recover completed video media.
- Newly submitted video job IDs are also copied into the page query string so they survive ordinary Streamlit redeploy/reload in the same browser URL.
- Improved dark-theme contrast for expander headers, uploader buttons, labels, captions, disabled buttons, cards, and inputs.


## R8 fix
- Fixed Flow async video polling: preserves literal `:` / `@` separators in useapi job IDs so `/jobs/{jobId}` no longer returns HTTP 400 Invalid job ID format.
- Existing submitted jobs can be recovered from the `flow_jobs` URL parameter or the recovery panel; do not resubmit merely because an older build failed to poll them.

## R9 exports + batch download
- Completed Omni videos render directly in **Results** when a signed preview URL is available; the app also exposes refresh/prepare-download controls per video.
- **Download entire batch** creates one ZIP containing `batch.csv`, `manifest.json`, completed try-on images, and completed MP4 videos.
- **Download product CSV** includes the original TikTok Shop product URL, product ID/name, image/video statuses, useapi media IDs/job ID, and generated URLs.
- Optional **Google Sheets push** supports replacing a worksheet tab or appending rows.

### Google Sheets setup
1. In Google Cloud, create a service account and enable the **Google Sheets API** and **Google Drive API** for its project.
2. Create/download a JSON key for that service account.
3. In Streamlit Cloud → App → Settings → Secrets, add the JSON as `GOOGLE_SERVICE_ACCOUNT_JSON` (see `.streamlit/secrets.example.toml`).
4. Open the target Google Sheet and share it with the service account's `client_email` as **Editor**.
5. In the app's Results → Export batch → Google Sheets section, paste the Sheet URL and press **Push batch to Google Sheet**.

Use **Replace tab** to keep one current batch snapshot, or **Append rows** to build a running log.

## R10 permanent Google Drive archive

R10 adds durable Google Drive copies of completed generated images and videos. This is separate from the Google Sheets service-account integration because Google service accounts do not have normal My Drive storage quota. For ordinary My Drive, R10 uses the included `google_drive_archiver.gs` Apps Script web app, which runs as your Google account and saves the files into a folder you own.

### What R10 does
- Automatically archives each completed generated image and completed MP4 when Drive archiving is configured and the sidebar toggle is on.
- Adds a manual **Archive completed media now** recovery/retry button in Results.
- Uses deterministic filenames and folders, so retries do not create duplicate files.
- Adds permanent Drive image/video links and Drive file IDs to CSV and Google Sheets exports.
- When `GOOGLE_SHEET_URL` is configured, automatic archive completion refreshes the `Flow Try-On` worksheet with the permanent Drive links.
- Adds an **Open batch folder in Google Drive** button after the first successful archive.
- Keeps the local full-batch ZIP workflow unchanged.

### Google Drive archive setup
1. In Google Drive, create a folder such as `Flow Try-On Archive`. Copy the folder ID from its URL.
2. Go to `script.google.com`, create a new Apps Script project, and replace the default code with the contents of `google_drive_archiver.gs`.
3. In Apps Script → Project Settings → Script Properties, add:
   - `ARCHIVE_FOLDER_ID` = your Drive folder ID
   - `ARCHIVE_SECRET` = a long random secret you choose
4. Deploy → New deployment → Web app:
   - Execute as: **Me**
   - Who has access: **Anyone**
5. Authorize the script when Google asks. Copy the `/exec` deployment URL.
6. In Streamlit Secrets add:
   - `GOOGLE_DRIVE_ARCHIVE_WEBHOOK_URL = ".../exec"`
   - `GOOGLE_DRIVE_ARCHIVE_SECRET = "the exact same secret"`
   - `GOOGLE_DRIVE_AUTO_ARCHIVE = "true"`
7. Redeploy/restart Streamlit. The sidebar should show **Drive archive · Ready**.

The shared secret is required because the Apps Script web app must accept server-to-server requests from Streamlit. Do not publish that secret or commit it to GitHub.
