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
