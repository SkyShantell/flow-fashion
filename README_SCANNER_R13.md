# R13.1 Scanner-Aware Build

This is the version to deploy when you are using the Creator Product Scanner handoff.

## Preserved from your scanner version

- `Import from Creator Scanner` panel
- `Scanner Queue` Google Sheet tab reader
- Add selected scanner products to the current batch
- Start a new batch from selected scanner products
- Mark queue rows imported after a successful SociaVault import

## Added from R13

- Image model changed to `nano-banana-pro`
- Academy-style image prompts with the phone locked over the face
- Academy-style Omni movement prompts adapted from the prompt pack
- Native Omni source remains `720p`
- Completed videos are automatically submitted through useapi `/videos/upscale` to `1080p`
- Downloads, ZIPs, Drive archive, and Google Sheet video links use the 1080p final

## No secret changes

You do not need to change Streamlit secrets, Google Sheet setup, Apps Script, or SociaVault/useapi keys.
