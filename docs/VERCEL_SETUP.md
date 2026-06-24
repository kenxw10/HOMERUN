# Vercel Setup

Use Vercel for the Next.js dashboard.

## Steps

1. Import the GitHub repo `kenxw10/HOMERUN` into Vercel.
2. Set the project root directory to `/apps/web`.
3. Set the frontend environment variable:

```powershell
NEXT_PUBLIC_API_BASE_URL=https://YOUR-RAILWAY-BACKEND-URL
```

4. Keep the default Next.js build command unless Vercel asks for one:

```powershell
npm run build
```

5. Deploy the dashboard.
6. Open the Vercel URL and confirm the first screen is the HOMERUN light-theme dashboard.

## Expected Dashboard State

For PR 1, the dashboard should show:

- Paper Mode badge.
- Live Trading Disabled badge.
- Empty portfolio value chart.
- Zero or `N/A` performance metrics.
- Empty contracts/positions table.
- Model status panel with no trained model.
- System status panel from the backend API.

If the API URL is wrong or the backend is down, the dashboard should show a clear API unavailable message.
