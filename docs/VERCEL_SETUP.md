# Vercel Setup

Use Vercel for the Next.js dashboard.

## Steps

1. Import the GitHub repo `kenxw10/HOMERUN` into Vercel.
2. Set the project root directory to `/apps/web`.
3. Set the frontend environment variables:

```powershell
NEXT_PUBLIC_API_BASE_URL=https://YOUR-RAILWAY-BACKEND-URL
NEXT_PUBLIC_REFRESH_MS=30000
```

4. Keep the default Next.js build command unless Vercel asks for one:

```powershell
npm run build
```

5. Deploy the dashboard.
6. Open the Vercel URL and confirm the first screen is the HOMERUN light-theme dashboard.

## Expected Dashboard State

For PR 2, the dashboard should show:

- Paper mode, live trading disabled, and kill switch on from the backend API.
- API connected or a clear API unavailable message.
- Portfolio value chart populated from database snapshots when they exist.
- Zero or `N/A` performance metrics.
- Open positions table with time entered, market, side, entry price, current price, quantity, P/L, status, and resolution columns.
- Model status panel with no trained model.
- System status panel from the backend API, including database readiness.

If the API URL is wrong or the backend is down, the dashboard should show a clear API unavailable message.
