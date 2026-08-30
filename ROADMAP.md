# Flow Try-On Factory Roadmap

R12 implements the selected production upgrades. These ideas are intentionally parked for later so the now-working Flow/useapi pipeline stays stable.

## Later

### Duplicate product protection
Before importing or generating, check Batch History for the same TikTok product ID/link and offer **Open existing assets** or **Generate again**. Goal: prevent accidental duplicate spend.

### Side-by-side reference approval
Add a faster QA view with the selected source/product reference on the left and generated try-on on the right, with **Approve / Reject / Regenerate** controls.

### VA-safe mode + Admin mode
Give VAs a simplified workflow containing only Import → References → Generate → Approve → Videos → Export. Hide model/provider/technical controls behind an Admin password or role.

### One-click Finish Batch
Add **Finish & Archive Batch** to verify all completed media is permanently archived, force the final Sheet/history sync, prepare the final ZIP, mark the batch complete, and clear the workspace for the next batch.
