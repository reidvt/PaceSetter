# Pushing Pacesetter to GitHub

Run these in **PowerShell** or **Git Bash** from the project folder.

## 0. One-time prerequisites

- Install Git: <https://git-scm.com/download/win>
- Set your identity (once per computer):

```powershell
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

## 1. Create the empty repo on GitHub

1. Go to <https://github.com/new>
2. Name it `pacesetter`, choose **Private** or **Public**
3. **Do not** add a README, .gitignore or license (the project already has them)
4. Click **Create repository** and copy the URL, e.g. `https://github.com/<your-username>/pacesetter.git`

## 2. Initialise and check what will be committed

```powershell
cd "C:\Users\reidv\OneDrive\Documents\Claude\Projects\Pacesetter"
git init -b main
git add .
git status
```

**Before committing, confirm none of these appear in the `git status` list:**

`.env` · `.spotify_token_cache` · `.strava_token_cache` · `pacesetter.db` (or `-shm` / `-wal`) · `__pycache__/`

If any do, stop: something is wrong with `.gitignore`. You can double-check with:

```powershell
git check-ignore -v .env .spotify_token_cache .strava_token_cache pacesetter.db
```

Each file should print the `.gitignore` rule that excludes it.

## 3. Commit and push

```powershell
git commit -m "Initial commit: Pacesetter prototype (sync, course analysis, model scaffolding)"
git remote add origin https://github.com/<your-username>/pacesetter.git
git push -u origin main
```

On the first push, Git for Windows opens a browser window to sign in to GitHub. Approve it and the push continues.

## 4. Future updates

```powershell
git add .
git commit -m "Describe what changed"
git push
```

## If a secret was ever pushed by mistake

Removing the file in a new commit does **not** remove it from history. Rotate the key right away:
Spotify → developer dashboard → your app → *Rotate client secret*; Strava → API settings → *Reset client secret*. Then update `.env`.
