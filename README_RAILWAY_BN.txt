ARAFAT FF OUTFIT API - RAILWAY DEPLOY
=====================================

IMPORTANT:
The supplied source contains an Info API and a banner/outfit renderer, but its
Info API's GetPlayerPersonalShow response is not proven to contain the
GetOutfit/ProfileInfo inventory fields required by the renderer. The source
also includes GetOutfit_pb2.py but no confirmed GetOutfit HTTP request path.
So /outfit/all may return an empty/missing outfit until that game endpoint
adapter is confirmed. Do not treat this as fully verified.

DEPLOY:
1. Upload this extracted project to a GitHub repository.
2. Railway -> New Project -> Deploy from GitHub Repo.
3. Railway reads requirements.txt and Procfile.
4. Add Variables (use your own authorized service-account credentials):
   FF_BD_UID
   FF_BD_PASSWORD
   FF_IND_UID
   FF_IND_PASSWORD
   FF_BR_UID
   FF_BR_PASSWORD
   Only set the regions you are authorized to use.
5. Deploy, then test:
   /
   /info?uid=PLAYER_UID
   /outfit/all?uid=PLAYER_UID&server=BD

SECURITY:
- Credentials from the uploaded source were not copied into this project.
- Never put account passwords in source code or public GitHub.
- The original Info API's /info endpoint uses its own service accounts.
- Do not expose this API publicly without rate limiting and access controls.

CHECKS:
Python syntax compilation was performed locally. Live Garena authentication,
Railway deployment, and the undocumented GetOutfit endpoint were not verified.
