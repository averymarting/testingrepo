# Netlify Redirect Deploy via GitHub Actions

No local Node/Netlify CLI required — everything runs on GitHub's runner.

## One-time setup

1. Create a new GitHub repo and push this folder to it:
   ```bash
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. Add your Netlify token as a **repository secret** (NOT as a workflow input —
   inputs are visible in the Actions UI/logs, secrets are encrypted and masked):
   - Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NETLIFY_AUTH_TOKEN`
   - Value: a fresh Netlify personal access token
     (User settings → Applications → Personal access tokens, in Netlify)

   > Since you've pasted tokens into a chat before, generate a brand-new one
   > specifically for this and don't reuse an old one.

## Running a deploy

1. Go to the repo → **Actions** tab → **Deploy Netlify Redirect Site** → **Run workflow**
2. Fill in the prompted fields:
   - `site_name` — must be globally unique across all Netlify, e.g. `tanveer-demo-1`
   - `default_url` — fallback destination
   - `partner1_url` / `partner2_url` — per-referral destinations
3. Click **Run workflow**

The job will:
- Install Node + Netlify CLI on the runner
- Generate `edge-functions/redirect.js` from the URLs you entered
- Create the Netlify site if it doesn't exist yet
- Deploy `public/` + the edge function to production
- Print the live URL to test, e.g.
  `https://<site_name>.netlify.app/go?ref=partner1`

## Notes

- Site names are global on Netlify — pick something unique or the create step
  will fail with a naming conflict.
- If you re-run with the same `site_name`, the workflow reuses the existing
  site instead of erroring.
- Swap `default_url`/`partner1_url`/`partner2_url` any time by re-running the
  workflow with new inputs — no code changes needed.
