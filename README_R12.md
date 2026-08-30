# R12 changes

Implemented now:
1. Permanent Batch History + reopen old batches
2. Retry failed only
4. Better batch dashboard + status filters
5. Automatic Drive archiving
6. Product/date Drive folders with references / try-ons / videos
7. Regenerate with operator instructions
10. Automatic Google Sheet production sync
11. Usage + retry/failure tracking and optional cost estimate

Saved for later in `ROADMAP.md`:
- duplicate product protection
- side-by-side reference approval
- VA-safe/Admin mode
- one-click Finish Batch

**Drive folder structure requires updating the existing Apps Script with the included `google_drive_archiver.gs` and deploying a new version.** The Streamlit secrets can stay unchanged if the deployment URL stays the same.
